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

"""Tests that run the stack programs under Pulumi mocks (see conftest.py)."""

import itertools
import json
import pathlib
import re
import runpy
import types

import pulumi
import pulumi_aws as aws
import pulumi_awsx as awsx
import pytest
import yaml

from ord_infrastructure import shared

STACKS = pathlib.Path(__file__).parent.parent / "stacks"
WORKFLOWS = pathlib.Path(__file__).parent.parent / ".github" / "workflows"

# ord-app's image build fails unless every one of these is set
# (frontend/apps/editor/vite.config.ts in ord-app).
AUTH0_BUILD_ARGS = (
    "VITE_AUTH0_DOMAIN",
    "VITE_AUTH0_CLIENT_ID",
    "VITE_AUTH0_AUDIENCE",
    "VITE_AUTH0_ISSUER",
    "VITE_AUTH0_SCOPE",
)


@pytest.fixture
def services(monkeypatch) -> list[tuple[pulumi.Resource, dict[str, object]]]:
    """Collects each service a stack program creates, with the arguments it was made with."""
    created = []
    make_web_service = shared.make_web_service

    def recording_make_web_service(**kwargs: object) -> pulumi.Resource:
        service = make_web_service(**kwargs)  # ty: ignore[invalid-argument-type]
        created.append((service, kwargs))
        return service

    monkeypatch.setattr(shared, "make_web_service", recording_make_web_service)
    return created


@pulumi.runtime.test
def test_editor_stack_passes_auth0_settings_to_build_and_task(
    pulumi_mocks, services, monkeypatch
):
    # The clean-main gate would inspect the developer's ord-app checkout.
    pulumi.runtime.set_all_config({"editor:enforce_clean": "false"})
    monkeypatch.chdir(STACKS / "editor")
    runpy.run_path("__main__.py")

    def check(_: object) -> None:
        build_args = pulumi_mocks.inputs_of("awsx:ecr:Image")["args"]
        assert all(build_args.get(name) for name in AUTH0_BUILD_ARGS)
        # The tenant and client come from the auth stack, which owns the client.
        auth = pulumi_mocks.STACK_OUTPUTS["ord/auth/prod"]
        tenant = auth["domain"]
        assert build_args["VITE_AUTH0_DOMAIN"] == tenant
        assert build_args["VITE_AUTH0_CLIENT_ID"] == auth["ord_app_client_id"]
        assert build_args["VITE_AUTH0_AUDIENCE"] == f"https://{tenant}/api/v2/"
        assert build_args["VITE_AUTH0_ISSUER"] == f"https://{tenant}/"

        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        environment = {
            variable["name"]: variable["value"]
            for variable in service["taskDefinitionArgs"]["container"]["environment"]
        }
        # The backend verifies tokens against the same tenant the UI signs in with.
        for name in ("VITE_AUTH0_DOMAIN", "VITE_AUTH0_AUDIENCE", "VITE_AUTH0_ISSUER"):
            assert environment[name] == build_args[name]

    ((service, _),) = services
    return service.urn.apply(check)


@pytest.mark.parametrize("missing", ["domain", "ord_app_client_id"])
def test_editor_stack_fails_without_an_auth_output(
    missing, pulumi_mocks, services, monkeypatch
):
    outputs = dict(pulumi_mocks.STACK_OUTPUTS["ord/auth/prod"])
    del outputs[missing]
    monkeypatch.setitem(pulumi_mocks.STACK_OUTPUTS, "ord/auth/prod", outputs)

    @pulumi.runtime.test
    def run() -> pulumi.Output:
        pulumi.runtime.set_all_config({"editor:enforce_clean": "false"})
        monkeypatch.chdir(STACKS / "editor")
        runpy.run_path("__main__.py")
        ((service, _),) = services
        return service.urn

    with pytest.raises(Exception, match=missing):
        run()


@pulumi.runtime.test
def test_editor_stack_gives_the_task_a_download_link_key(
    pulumi_mocks, services, monkeypatch
):
    pulumi.runtime.set_all_config({"editor:enforce_clean": "false"})
    monkeypatch.chdir(STACKS / "editor")
    runpy.run_path("__main__.py")

    def check(_: object) -> None:
        secret_arn = "arn:aws:secretsmanager:secret/download_link_secret"
        version = pulumi_mocks.inputs_of(
            "aws:secretsmanager/secretVersion:SecretVersion"
        )
        # The generated key stays a Pulumi secret, so it arrives wrapped.
        assert version["secretString"]["value"] == "generated"
        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        secrets = {
            secret["name"]: secret["valueFrom"]
            for secret in service["taskDefinitionArgs"]["container"]["secrets"]
        }
        assert secrets["DOWNLOAD_LINK_SECRET"] == secret_arn
        # The execution role must be able to read every secret the task references.
        policy = pulumi_mocks.inputs_of("aws:iam/rolePolicy:RolePolicy")["policy"]
        assert secret_arn in policy

    ((service, arguments),) = services
    # ECS reads the secret when the task starts, so its value must exist first.
    assert any(
        isinstance(resource, aws.secretsmanager.SecretVersion)
        for resource in arguments["depends_on"]
    )
    return service.urn.apply(check)


@pytest.mark.parametrize("pulumi_mocks", ["editor", "viewer"], indirect=True)
@pulumi.runtime.test
def test_service_stacks_keep_container_logs_for_90_days(
    pulumi_mocks, services, monkeypatch
):
    # The clean-main gate would inspect the developer's ord-app checkout.
    monkeypatch.setenv("PULUMI_ALLOW_DIRTY", "1")
    pulumi.runtime.set_all_config({})
    monkeypatch.chdir(STACKS / pulumi.get_project())
    runpy.run_path("__main__.py")

    def check(_: object) -> None:
        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        log_group = service["taskDefinitionArgs"]["logGroup"]["args"]
        # Pinned rather than bounded: a shorter period deletes the events already older
        # than it, and 0 keeps every event.
        assert log_group["retentionInDays"] == 90

    ((service, _),) = services
    return service.urn.apply(check)


def _run_viewer_stack(monkeypatch, config: dict[str, str] | None = None) -> None:
    """Run the viewer's stack program under the mocks, with `config` as its config."""
    # The clean-main gate would inspect the developer's ord-app checkout.
    monkeypatch.setenv("PULUMI_ALLOW_DIRTY", "1")
    pulumi.runtime.set_all_config(config or {})
    monkeypatch.chdir(STACKS / "viewer")
    runpy.run_path("__main__.py")


@pytest.mark.parametrize("pulumi_mocks", ["viewer"], indirect=True)
@pulumi.runtime.test
def test_viewer_stack_serves_a_pinned_corpus(pulumi_mocks, services, monkeypatch):
    _run_viewer_stack(monkeypatch)

    def check(_: object) -> None:
        image = pulumi_mocks.inputs_of("awsx:ecr:Image")
        assert image["dockerfile"] == "../../../ord-app/deploy/viewer/Dockerfile"
        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        container = service["taskDefinitionArgs"]["container"]
        environment = {
            variable["name"]: variable["value"] for variable in container["environment"]
        }
        assert environment["ORD_VIEWER_CORPUS_DIR"] == "/app/corpus"
        # Commits, so every task fetches the same corpus; the viewer refuses one
        # revision without the other.
        for name in ("ORD_VIEWER_ARTIFACTS_REVISION", "ORD_VIEWER_SOURCES_REVISION"):
            assert re.fullmatch("[0-9a-f]{40}", environment[name])
        # DuckDB's cap leaves the rest of the process room within the task's memory.
        memory_limit = re.fullmatch(
            "([0-9]+)MiB", environment["ORD_VIEWER_MEMORY_LIMIT"]
        )
        assert memory_limit
        assert int(memory_limit[1]) < container["memory"]
        # The corpus is fetched onto the task's storage, so it gets more than Fargate's
        # default.
        storage = service["taskDefinitionArgs"]["ephemeralStorage"]["sizeInGib"]
        assert storage > 20
        # The API answers its health check only once the corpus is fetched and open.
        assert service["healthCheckGracePeriodSeconds"] > 0
        target_group = pulumi_mocks.inputs_of("aws:lb/targetGroup:TargetGroup")
        assert target_group["healthCheck"]["path"] == "/api/health"
        # The viewer serves the apex. The alias registers after the service, so the
        # rule names the host.
        rule = pulumi_mocks.inputs_of("aws:lb/listenerRule:ListenerRule")
        (condition,) = rule["conditions"]
        assert condition["hostHeader"]["values"] == ["example.com"]

    ((service, _),) = services
    return service.urn.apply(check)


@pytest.mark.parametrize("pulumi_mocks", ["viewer"], indirect=True)
@pulumi.runtime.test
def test_viewer_stack_needs_no_config_secrets(pulumi_mocks, services, monkeypatch):
    _run_viewer_stack(monkeypatch)

    def check(_: object) -> None:
        assert not [
            resource
            for resource in pulumi_mocks.resources
            if resource.typ.startswith("aws:secretsmanager/")
        ]
        # With no secret to read, the execution role gets no inline policy.
        assert not [
            resource
            for resource in pulumi_mocks.resources
            if resource.typ == "aws:iam/rolePolicy:RolePolicy"
        ]
        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        assert not service["taskDefinitionArgs"]["container"].get("secrets")

    ((service, _),) = services
    return service.urn.apply(check)


@pytest.mark.parametrize(
    ("key", "variable"),
    [("anthropic_api_key", "ORD_ANTHROPIC_API_KEY"), ("hf_token", "HF_TOKEN")],
)
@pytest.mark.parametrize("pulumi_mocks", ["viewer"], indirect=True)
@pulumi.runtime.test
def test_viewer_stack_injects_a_configured_secret(
    key, variable, pulumi_mocks, services, monkeypatch
):
    _run_viewer_stack(monkeypatch, {f"viewer:{key}": "s3cret"})

    def check(_: object) -> None:
        secret_arn = f"arn:aws:secretsmanager:secret/{key}_secret"
        version = pulumi_mocks.inputs_of(
            "aws:secretsmanager/secretVersion:SecretVersion"
        )
        # A config secret stays a Pulumi secret, so it arrives wrapped.
        assert version["secretString"]["value"] == "s3cret"
        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        secrets = {
            secret["name"]: secret["valueFrom"]
            for secret in service["taskDefinitionArgs"]["container"]["secrets"]
        }
        # Pinned to the version, so a new value changes the task definition.
        assert secrets == {variable: f"{secret_arn}:::{key}_version-version-id"}
        # The role reads the secret by its ARN, whichever version a task names.
        policy = pulumi_mocks.inputs_of("aws:iam/rolePolicy:RolePolicy")["policy"]
        assert json.loads(policy)["Statement"][0]["Resource"] == [secret_arn]

    ((service, _),) = services
    return service.urn.apply(check)


@pytest.mark.parametrize("pulumi_mocks", ["viewer"], indirect=True)
@pulumi.runtime.test
def test_viewer_stack_injects_every_configured_secret(
    pulumi_mocks, services, monkeypatch
):
    _run_viewer_stack(
        monkeypatch, {"viewer:anthropic_api_key": "key", "viewer:hf_token": "token"}
    )

    def check(_: object) -> None:
        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        secrets = service["taskDefinitionArgs"]["container"]["secrets"]
        assert {secret["name"] for secret in secrets} == {
            "ORD_ANTHROPIC_API_KEY",
            "HF_TOKEN",
        }
        policy = pulumi_mocks.inputs_of("aws:iam/rolePolicy:RolePolicy")["policy"]
        assert sorted(json.loads(policy)["Statement"][0]["Resource"]) == [
            "arn:aws:secretsmanager:secret/anthropic_api_key_secret",
            "arn:aws:secretsmanager:secret/hf_token_secret",
        ]

    ((service, _),) = services
    return service.urn.apply(check)


class _Vpc:
    """Stands in for awsx.ec2.Vpc, whose outputs the mocks cannot construct.

    Its ``vpc`` output is a reference to a child resource the mocks never create.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.vpc_id = pulumi.Output.from_input("vpc-0")
        self.vpc = types.SimpleNamespace(
            cidr_block=pulumi.Output.from_input("10.0.0.0/16")
        )
        # One subnet of each kind per availability zone the backend spans.
        self.private_subnet_ids = pulumi.Output.from_input(
            ["subnet-0", "subnet-1", "subnet-2"]
        )
        self.public_subnet_ids = pulumi.Output.from_input(
            ["subnet-3", "subnet-4", "subnet-5"]
        )


@pytest.mark.parametrize("pulumi_mocks", ["backend"], indirect=True)
@pulumi.runtime.test
def test_backend_stack_question_log_expiry_and_trail_agree(pulumi_mocks, monkeypatch):
    monkeypatch.setattr(awsx.ec2, "Vpc", _Vpc)
    monkeypatch.chdir(STACKS / "backend")
    program = runpy.run_path("__main__.py")
    account = pulumi_mocks.CALL_RESULTS["aws:index/getCallerIdentity:getCallerIdentity"]
    region = pulumi_mocks.CALL_RESULTS["aws:index/getRegion:getRegion"]
    bucket_arn = "arn:aws:s3:::open-reaction-database-logs"

    def check(_: object) -> None:
        rules = pulumi_mocks.inputs_of(
            "aws:s3/bucketLifecycleConfiguration:BucketLifecycleConfiguration"
        )["rules"]
        expiring = [rule for rule in rules if "expiration" in rule]
        expiry = {
            rule["filter"]["prefix"]: rule["expiration"]["days"] for rule in expiring
        }
        # On a versioned bucket, expiring an object leaves its data in the version
        # beneath, so each rule expires noncurrent versions too.
        assert all(rule["noncurrentVersionExpiration"] for rule in expiring)
        # Where expiration rules overlap, S3 applies the shorter one.
        for prefix, other in itertools.permutations(expiry, 2):
            assert not other.startswith(prefix)
        # The raw prefix is the one ord_schema.search.nl_log.S3Sink writes by default.
        raw, redacted = expiry["nl-log/raw/"], expiry["nl-log/redacted/"]
        trail = pulumi_mocks.inputs_of("aws:cloudtrail/trail:Trail")
        audit = expiry[f"{trail['s3KeyPrefix']}/"]
        assert raw < redacted < audit

        (selector,) = trail["advancedEventSelectors"]
        fields = {field["field"]: field for field in selector["fieldSelectors"]}
        (watched,) = fields["resources.ARN"]["startsWiths"]
        assert all(
            f"{bucket_arn}/{prefix}".startswith(watched)
            for prefix in ("nl-log/raw/", "nl-log/redacted/")
        )
        # The trail's own deliveries fall outside what it watches.
        assert not f"{bucket_arn}/{trail['s3KeyPrefix']}/".startswith(watched)
        assert fields["eventName"]["notEquals"] == ["PutObject"]

        # CloudTrail may deliver only where this trail writes, and only for this trail.
        trail_arn = (
            f"arn:aws:cloudtrail:{region['region']}:{account['accountId']}"
            f":trail/{trail['name']}"
        )
        policy = json.loads(
            pulumi_mocks.inputs_of("aws:s3/bucketPolicy:BucketPolicy")["policy"]
        )
        for statement in policy["Statement"]:
            assert statement["Condition"]["StringEquals"]["aws:SourceArn"] == trail_arn
        (put,) = [s for s in policy["Statement"] if s["Action"] == "s3:PutObject"]
        assert put["Resource"] == (
            f"{bucket_arn}/{trail['s3KeyPrefix']}/AWSLogs/{account['accountId']}/*"
        )

    return program["question_log_trail"].urn.apply(check)


@pytest.mark.parametrize("pulumi_mocks", ["account"], indirect=True)
@pulumi.runtime.test
def test_account_stack_lets_only_the_deploy_environments_assume_the_deploy_role(
    pulumi_mocks, monkeypatch
):
    pulumi.runtime.set_all_config({})
    monkeypatch.chdir(STACKS / "account")
    role = runpy.run_path("__main__.py")["deploy_role"]

    def check(_: object) -> None:
        provider = pulumi_mocks.inputs_of(
            "aws:iam/openIdConnectProvider:OpenIdConnectProvider"
        )
        assert provider["url"] == "https://token.actions.githubusercontent.com"
        (inputs,) = [
            r.inputs for r in pulumi_mocks.resources if r.name == "deploy_role"
        ]
        (statement,) = json.loads(inputs["assumeRolePolicy"])["Statement"]
        assert statement["Principal"] == {
            "Federated": "arn:aws:iam::123456789012:oidc-provider/github_oidc_provider"
        }
        assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
        # Exact matches only: a wildcard subject would let another repository, or a job
        # outside the environments that only main may use, deploy.
        assert statement["Condition"] == {
            "StringEquals": {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                "token.actions.githubusercontent.com:sub": [
                    "repo:open-reaction-database/ord-infrastructure:environment:prod",
                    "repo:open-reaction-database/ord-infrastructure:environment:staging",
                ],
            }
        }

    return role.urn.apply(check)


@pytest.mark.parametrize("pulumi_mocks", ["account"], indirect=True)
@pulumi.runtime.test
def test_account_stack_lets_only_the_deploy_environments_get_pulumi_tokens(
    pulumi_mocks, monkeypatch
):
    pulumi.runtime.set_all_config({})
    monkeypatch.chdir(STACKS / "account")
    issuer = runpy.run_path("__main__.py")["pulumi_oidc_issuer"]

    def check(_: object) -> None:
        inputs = pulumi_mocks.inputs_of("pulumiservice:index:OidcIssuer")
        assert inputs["url"] == "https://token.actions.githubusercontent.com"
        assert inputs["organization"] == "ord"
        assert inputs["policies"] == [
            {
                "decision": "allow",
                "tokenType": "organization",
                "authorizedPermissions": ["admin"],
                "rules": {
                    "aud": "urn:pulumi:org:ord",
                    "sub": f"repo:open-reaction-database/ord-infrastructure:environment:{environment}",
                },
            }
            for environment in ("prod", "staging")
        ]
        # Pulumi Cloud reads *, ?, and . in a rule as wildcards, so these rules match
        # their claims exactly.
        for policy in inputs["policies"]:
            assert not re.search(r"[*?.]", "".join(policy["rules"].values()))

    return issuer.urn.apply(check)


def _workflow_environments(path: pathlib.Path) -> set[str]:
    """Returns every deployment environment the jobs of a workflow can run in.

    Args:
        path: A workflow whose jobs each name an environment, either literally or as
            ``${{ inputs.<name> }}`` for a choice input of its ``workflow_dispatch``.

    Returns:
        The environment names, with each input expanded to its options.
    """
    workflow = yaml.safe_load(path.read_text())
    # YAML 1.1 reads the bare key `on` as True.
    inputs = workflow[True]["workflow_dispatch"].get("inputs", {})
    environments = set()
    for job in workflow["jobs"].values():
        environment = job["environment"]
        match = re.fullmatch(r"\$\{\{ inputs\.(\w+) \}\}", environment)
        environments.update(inputs[match[1]]["options"] if match else [environment])
    return environments


@pytest.mark.parametrize("pulumi_mocks", ["account"], indirect=True)
@pulumi.runtime.test
def test_deploy_workflows_run_only_in_environments_the_deploy_role_trusts(
    pulumi_mocks, monkeypatch
):
    pulumi.runtime.set_all_config({})
    monkeypatch.chdir(STACKS / "account")
    trusted = runpy.run_path("__main__.py")["DEPLOY_ENVIRONMENTS"]
    workflows = sorted(WORKFLOWS.glob("deploy_*.yml"))
    assert [workflow.name for workflow in workflows] == [
        "deploy_editor.yml",
        "deploy_viewer.yml",
    ]
    for workflow in workflows:
        # A job outside them could not assume the role, so its deploy would fail.
        assert _workflow_environments(workflow) <= set(trusted), workflow.name


@pytest.mark.parametrize("pulumi_mocks", ["account"], indirect=True)
@pulumi.runtime.test
def test_deploy_workflows_request_the_tokens_the_pulumi_issuer_grants(
    pulumi_mocks, monkeypatch
):
    pulumi.runtime.set_all_config({})
    monkeypatch.chdir(STACKS / "account")
    organization = runpy.run_path("__main__.py")["PULUMI_ORGANIZATION"]
    for workflow in sorted(WORKFLOWS.glob("deploy_*.yml")):
        (job,) = yaml.safe_load(workflow.read_text())["jobs"].values()
        (step,) = [
            step
            for step in job["steps"]
            if step.get("uses", "").startswith("pulumi/auth-actions@")
        ]
        # The organization names the audience of the job's GitHub token, which the
        # issuer's policies match.
        assert step["with"] == {
            "organization": organization,
            "requested-token-type": "urn:pulumi:token-type:access_token:organization",
        }, workflow.name
