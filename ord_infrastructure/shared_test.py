# Copyright 2026 Open Reaction Database Project Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the deploy-time guards in ord_infrastructure.shared.

These cover the helpers that gate what ships to production — the sibling-repo
cleanliness check, the image provenance stamp, and the ALB name-length and redirect
guards — and, under Pulumi mocks (see conftest.py), what make_web_service passes to the
image build, the task, and the load balancer.
"""

import json
import pathlib
import subprocess
from collections.abc import Sequence
from typing import cast

import pulumi
import pulumi_aws as aws
import pulumi_awsx as awsx
import pytest

from ord_infrastructure.shared import (
    assert_sibling_clean,
    make_web_service,
    sibling_head,
)


def _git(path: pathlib.Path, *args: str) -> str:
    """Run a git command in `path` and return its stdout."""
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch, tmp_path):
    # A developer with PULUMI_ALLOW_DIRTY exported would otherwise short-circuit
    # every assert_sibling_clean case.
    monkeypatch.delenv("PULUMI_ALLOW_DIRTY", raising=False)
    # Keep git's repository discovery from walking out of tmp_path and finding an
    # enclosing repo, which would make the "not a git repo" case pass by accident.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


@pytest.fixture
def sibling(tmp_path) -> pathlib.Path:
    """A clean repo on `main`, in sync with a local bare `origin`."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        capture_output=True,
        check=True,
    )
    repo = tmp_path / "sibling"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        capture_output=True,
        check=True,
    )
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "--quiet", "origin", "main")
    return repo


def test_assert_sibling_clean_accepts_clean_repo_in_sync(sibling):
    assert_sibling_clean(str(sibling))


def test_assert_sibling_clean_accepts_non_default_branch(sibling):
    _git(sibling, "checkout", "--quiet", "-b", "release")
    _git(sibling, "push", "--quiet", "origin", "release")
    assert_sibling_clean(str(sibling), branch="release")


def test_assert_sibling_clean_rejects_uncommitted_changes(sibling):
    (sibling / "README.md").write_text("modified\n")
    with pytest.raises(SystemExit, match="uncommitted changes"):
        assert_sibling_clean(str(sibling))


def test_assert_sibling_clean_rejects_untracked_files(sibling):
    (sibling / "scratch.txt").write_text("untracked\n")
    with pytest.raises(SystemExit, match="untracked files"):
        assert_sibling_clean(str(sibling))


def test_assert_sibling_clean_accepts_ignored_files(sibling):
    (sibling / ".gitignore").write_text("scratch.txt\n")
    _git(sibling, "add", ".gitignore")
    _git(sibling, "commit", "--quiet", "-m", "ignore scratch")
    _git(sibling, "push", "--quiet", "origin", "main")
    (sibling / "scratch.txt").write_text("ignored\n")
    assert_sibling_clean(str(sibling))
    assert sibling_head(str(sibling)) == _git(sibling, "rev-parse", "HEAD")


def test_assert_sibling_clean_rejects_a_tree_git_cannot_read(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    with pytest.raises(SystemExit, match="uncommitted changes or untracked files"):
        assert_sibling_clean(str(not_a_repo))


def test_assert_sibling_clean_rejects_wrong_branch(sibling):
    _git(sibling, "checkout", "--quiet", "-b", "feature")
    with pytest.raises(SystemExit, match="is on 'feature', expected 'main'"):
        assert_sibling_clean(str(sibling))


def test_assert_sibling_clean_rejects_unpushed_commit(sibling):
    (sibling / "README.md").write_text("second\n")
    _git(sibling, "commit", "--quiet", "--all", "-m", "second")
    with pytest.raises(SystemExit, match="but origin/main is at"):
        assert_sibling_clean(str(sibling))


def test_assert_sibling_clean_rejects_unreachable_origin(sibling, tmp_path):
    _git(sibling, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    with pytest.raises(SystemExit, match="failed to fetch"):
        assert_sibling_clean(str(sibling))


def test_assert_sibling_clean_allows_override_when_dirty(sibling, monkeypatch):
    monkeypatch.setenv("PULUMI_ALLOW_DIRTY", "1")
    (sibling / "README.md").write_text("modified\n")
    _git(sibling, "checkout", "--quiet", "-b", "feature")
    assert_sibling_clean(str(sibling))


def test_sibling_head_returns_head_sha_when_clean(sibling):
    assert sibling_head(str(sibling)) == _git(sibling, "rev-parse", "HEAD")


def test_sibling_head_counts_modified_file_as_dirty(sibling):
    (sibling / "README.md").write_text("modified\n")
    head = sibling_head(str(sibling))
    assert head == f"{_git(sibling, 'rev-parse', 'HEAD')}-dirty"


def test_sibling_head_counts_untracked_file_as_dirty(sibling):
    (sibling / "scratch.txt").write_text("untracked\n")
    assert sibling_head(str(sibling)).endswith("-dirty")


def test_sibling_head_returns_unknown_outside_a_repo(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    assert sibling_head(str(not_a_repo)) == "unknown"


def _web_service(
    sibling: pathlib.Path,
    *,
    secret_arns: Sequence[str] = (),
    log_retention_days: int = 30,
    **kwargs,
) -> awsx.ecs.FargateService:
    """Call make_web_service on `sibling`, adding `kwargs` to the shared arguments."""
    return make_web_service(
        backend=pulumi.StackReference("ord/backend/prod"),
        domain=pulumi.StackReference("ord/domain/prod"),
        container_port=5173,
        record_name="app.example.com",
        listener_rule_priority=200,
        health_check_path="/api/v1/health",
        sibling_path=str(sibling),
        dockerfile=str(sibling / "Dockerfile"),
        secret_arns=secret_arns,
        log_retention_days=log_retention_days,
        enforce_clean=False,
        **kwargs,
    )


@pulumi.runtime.test
def test_make_web_service_passes_build_args_and_environment(sibling, pulumi_mocks):
    service = _web_service(
        sibling,
        environment=[
            awsx.ecs.TaskDefinitionKeyValuePairArgs(name="VITE_AUTH0_DOMAIN", value="d")
        ],
        # GIT_COMMIT is always the sibling's HEAD; a caller's value cannot replace it.
        build_args={"VITE_AUTH0_DOMAIN": "d", "GIT_COMMIT": "spoofed"},
    )

    def check(_: object) -> None:
        image = pulumi_mocks.inputs_of("awsx:ecr:Image")
        assert image["args"] == {
            "VITE_AUTH0_DOMAIN": "d",
            "GIT_COMMIT": _git(sibling, "rev-parse", "HEAD"),
        }
        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        container = service["taskDefinitionArgs"]["container"]
        assert container["environment"] == [{"name": "VITE_AUTH0_DOMAIN", "value": "d"}]

    # Resources register asynchronously. The service's inputs include the image's URI, so
    # by the time its URN resolves, both have registered.
    return service.urn.apply(check)  # ty: ignore[missing-argument, invalid-argument-type]


@pulumi.runtime.test
def test_make_web_service_builds_for_the_task_architecture(sibling, pulumi_mocks):
    service = _web_service(sibling)

    def check(_: object) -> None:
        image = pulumi_mocks.inputs_of("awsx:ecr:Image")
        assert image["platform"] == "linux/arm64"
        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        assert service["taskDefinitionArgs"]["runtimePlatform"] == {
            "cpuArchitecture": "ARM64",
            "operatingSystemFamily": "LINUX",
        }

    return service.urn.apply(check)  # ty: ignore[missing-argument, invalid-argument-type]


@pulumi.runtime.test
def test_make_web_service_keeps_recent_images(sibling, pulumi_mocks):
    service = _web_service(sibling)

    def check(_: object) -> None:
        repository = pulumi_mocks.inputs_of("awsx:ecr:Repository")
        assert repository["lifecyclePolicy"]["rules"] == [
            {
                "tagStatus": "untagged",
                "maximumNumberOfImages": 1,
                "description": "remove untagged images",
            },
            {
                "tagStatus": "any",
                "maximumNumberOfImages": 5,
                "description": "keep the 5 most recent images",
            },
        ]

    return service.urn.apply(check)  # ty: ignore[missing-argument, invalid-argument-type]


@pulumi.runtime.test
def test_make_web_service_passes_grace_period_and_storage(sibling, pulumi_mocks):
    service = _web_service(
        sibling, health_check_grace_period_seconds=600, ephemeral_storage_gib=30
    )

    def check(_: object) -> None:
        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        assert service["healthCheckGracePeriodSeconds"] == 600
        storage = service["taskDefinitionArgs"]["ephemeralStorage"]
        assert storage == {"sizeInGib": 30}

    return service.urn.apply(check)  # ty: ignore[missing-argument, invalid-argument-type]


@pulumi.runtime.test
def test_make_web_service_rolls_back_a_failing_deployment(sibling, pulumi_mocks):
    service = _web_service(sibling)

    def check(_: object) -> None:
        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        assert service["deploymentCircuitBreaker"] == {"enable": True, "rollback": True}

    return service.urn.apply(check)  # ty: ignore[missing-argument, invalid-argument-type]


@pulumi.runtime.test
def test_make_web_service_leaves_grace_period_and_storage_to_aws(sibling, pulumi_mocks):
    service = _web_service(sibling)

    def check(_: object) -> None:
        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        assert "healthCheckGracePeriodSeconds" not in service
        assert "ephemeralStorage" not in service["taskDefinitionArgs"]

    return service.urn.apply(check)  # ty: ignore[missing-argument, invalid-argument-type]


@pulumi.runtime.test
def test_make_web_service_sets_log_retention(sibling, pulumi_mocks):
    service = _web_service(sibling, log_retention_days=14)

    def check(_: object) -> None:
        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        # Arguments to the group awsx creates, not an `existing` group, so the deployed
        # group keeps its URN.
        assert service["taskDefinitionArgs"]["logGroup"] == {
            "args": {"retentionInDays": 14}
        }

    return service.urn.apply(check)  # ty: ignore[missing-argument, invalid-argument-type]


@pulumi.runtime.test
def test_make_web_service_lets_the_role_read_each_secret(sibling, pulumi_mocks):
    service = _web_service(sibling, secret_arns=["arn:0", "arn:1"])

    def check(_: object) -> None:
        policy = pulumi_mocks.inputs_of("aws:iam/rolePolicy:RolePolicy")["policy"]
        (statement,) = json.loads(policy)["Statement"]
        assert statement["Resource"] == ["arn:0", "arn:1"]

    return service.urn.apply(check)  # ty: ignore[missing-argument, invalid-argument-type]


@pulumi.runtime.test
def test_make_web_service_gives_no_secrets_policy_without_secrets(
    sibling, pulumi_mocks
):
    service = _web_service(sibling)

    def check(_: object) -> None:
        # IAM refuses a statement with no resources.
        assert not [
            resource
            for resource in pulumi_mocks.resources
            if resource.typ == "aws:iam/rolePolicy:RolePolicy"
        ]

    return service.urn.apply(check)  # ty: ignore[missing-argument, invalid-argument-type]


@pulumi.runtime.test
def test_make_web_service_redirects_no_host_by_default(
    sibling, pulumi_mocks, constructed
):
    rules = constructed(aws.lb, "ListenerRule")
    records = constructed(aws.route53, "Record")
    _web_service(sibling)
    ((rule, _),) = rules
    assert len(records) == 1

    def check(_: object) -> None:
        inputs = pulumi_mocks.inputs_of("aws:lb/listenerRule:ListenerRule")
        (condition,) = inputs["conditions"]
        assert condition["hostHeader"]["values"] == ["app.example.com"]

    return rule.urn.apply(check)


@pulumi.runtime.test
def test_make_web_service_redirects_a_host_to_its_own(
    sibling, pulumi_mocks, constructed
):
    rules = constructed(aws.lb, "ListenerRule")
    records = constructed(aws.route53, "Record")
    _web_service(
        sibling, redirect_host="old.example.com", redirect_listener_rule_priority=190
    )
    (_, (redirect_rule, redirect_rule_args)) = rules
    ((alias, _), (_, redirect_alias_args)) = records
    # The redirect waits for the service's own host to resolve, and the redirect host
    # resolves only once the redirect answers it.
    assert redirect_rule_args["opts"].depends_on == [alias]
    assert redirect_alias_args["opts"].depends_on == [redirect_rule]

    def check(_: object) -> None:
        inputs = {
            resource.name: resource.inputs
            for resource in pulumi_mocks.resources
            if resource.typ
            in ("aws:lb/listenerRule:ListenerRule", "aws:route53/record:Record")
        }
        # The forward rule serves the redirect host until the redirect rule, which the
        # listener evaluates first, exists.
        forward = inputs["listener_rule"]
        (condition,) = forward["conditions"]
        assert condition["hostHeader"]["values"] == [
            "app.example.com",
            "old.example.com",
        ]
        redirect = inputs["redirect_listener_rule"]
        assert redirect["priority"] < forward["priority"]
        (condition,) = redirect["conditions"]
        assert condition["hostHeader"]["values"] == ["old.example.com"]
        (action,) = redirect["actions"]
        assert action["type"] == "redirect"
        assert action["redirect"] == {
            "host": "app.example.com",
            "statusCode": "HTTP_301",
        }
        assert inputs["alias"]["name"] == "app.example.com"
        assert inputs["redirect_alias"]["name"] == "old.example.com"
        assert inputs["redirect_alias"]["aliases"] == inputs["alias"]["aliases"]

    registered = pulumi.Output.all(*(resource.urn for resource, _ in rules + records))
    return registered.apply(check)  # ty: ignore[missing-argument, invalid-argument-type]


def _make_unbuilt_web_service(**kwargs) -> None:
    """Call make_web_service with no backend or domain, for a guard that runs first."""
    # The guards run before any resource is constructed, so this needs no Pulumi
    # runtime or mocks.
    make_web_service(
        backend=cast(pulumi.StackReference, None),
        domain=cast(pulumi.StackReference, None),
        container_port=8080,
        record_name="test.example.com",
        listener_rule_priority=100,
        health_check_path="/",
        sibling_path="../../../ord-app",
        dockerfile="../../../ord-app/Dockerfile",
        secret_arns=[],
        log_retention_days=30,
        **kwargs,
    )


def test_make_web_service_rejects_too_long_name_prefix():
    with pytest.raises(ValueError, match="too long"):
        _make_unbuilt_web_service(name_prefix="x" * 30)


@pytest.mark.parametrize("priority", [None, 100, 150])
def test_make_web_service_rejects_a_redirect_the_forward_rule_outranks(priority):
    with pytest.raises(
        ValueError, match=r"needs a redirect_listener_rule_priority below .*\(100\)"
    ):
        _make_unbuilt_web_service(
            redirect_host="old.example.com", redirect_listener_rule_priority=priority
        )
