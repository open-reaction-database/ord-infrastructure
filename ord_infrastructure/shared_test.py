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
cleanliness check, the image provenance stamp, and the ALB name-length guard — and,
under Pulumi mocks (see conftest.py), what make_web_service passes to the image build
and the task.
"""

import pathlib
import subprocess
from typing import cast

import pulumi
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


def test_assert_sibling_clean_ignores_untracked_files(sibling):
    # `git diff --quiet HEAD` does not see untracked files, so a new-but-unadded
    # file does not block a deploy. sibling_head deliberately differs (see
    # test_sibling_head_counts_untracked_file_as_dirty), because such a file can
    # still be COPYed into the image.
    (sibling / "scratch.txt").write_text("untracked\n")
    assert_sibling_clean(str(sibling))


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


@pulumi.runtime.test
def test_make_web_service_passes_build_args_and_environment(sibling, pulumi_mocks):
    service = make_web_service(
        backend=pulumi.StackReference("ord/backend/prod"),
        domain=pulumi.StackReference("ord/domain/prod"),
        container_port=5173,
        record_name="app.example.com",
        listener_rule_priority=200,
        health_check_path="/api/v1/health",
        sibling_path=str(sibling),
        dockerfile=str(sibling / "Dockerfile"),
        secret_arns=[],
        environment=[
            awsx.ecs.TaskDefinitionKeyValuePairArgs(name="VITE_AUTH0_DOMAIN", value="d")
        ],
        # GIT_COMMIT is always the sibling's HEAD; a caller's value cannot replace it.
        build_args={"VITE_AUTH0_DOMAIN": "d", "GIT_COMMIT": "spoofed"},
        enforce_clean=False,
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
def test_make_web_service_keeps_recent_images(sibling, pulumi_mocks):
    service = make_web_service(
        backend=pulumi.StackReference("ord/backend/prod"),
        domain=pulumi.StackReference("ord/domain/prod"),
        container_port=5173,
        record_name="app.example.com",
        listener_rule_priority=200,
        health_check_path="/api/v1/health",
        sibling_path=str(sibling),
        dockerfile=str(sibling / "Dockerfile"),
        secret_arns=[],
        enforce_clean=False,
    )

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


def test_make_web_service_rejects_too_long_name_prefix():
    # The guard runs before any resource is constructed, so this needs no Pulumi
    # runtime or mocks.
    with pytest.raises(ValueError, match="too long"):
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
            name_prefix="x" * 30,
        )
