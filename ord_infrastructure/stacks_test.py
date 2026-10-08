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

import pathlib
import runpy

import pulumi
import pulumi_aws as aws
import pytest

from ord_infrastructure import shared

STACKS = pathlib.Path(__file__).parent.parent / "stacks"

# ord-app's image build fails unless every one of these is set (ui/vite.config.ts).
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
def test_app_stack_passes_auth0_settings_to_build_and_task(
    pulumi_mocks, services, monkeypatch
):
    # The clean-main gate would inspect the developer's ord-app checkout.
    pulumi.runtime.set_all_config({"app:enforce_clean": "false"})
    monkeypatch.chdir(STACKS / "app")
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
def test_app_stack_fails_without_an_auth_output(
    missing, pulumi_mocks, services, monkeypatch
):
    outputs = dict(pulumi_mocks.STACK_OUTPUTS["ord/auth/prod"])
    del outputs[missing]
    monkeypatch.setitem(pulumi_mocks.STACK_OUTPUTS, "ord/auth/prod", outputs)

    @pulumi.runtime.test
    def run() -> pulumi.Output:
        pulumi.runtime.set_all_config({"app:enforce_clean": "false"})
        monkeypatch.chdir(STACKS / "app")
        runpy.run_path("__main__.py")
        ((service, _),) = services
        return service.urn

    with pytest.raises(Exception, match=missing):
        run()


@pulumi.runtime.test
def test_app_stack_gives_the_task_a_download_link_key(
    pulumi_mocks, services, monkeypatch
):
    pulumi.runtime.set_all_config({"app:enforce_clean": "false"})
    monkeypatch.chdir(STACKS / "app")
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
