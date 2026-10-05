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
def services(monkeypatch) -> list[pulumi.Resource]:
    """Collects the services a stack program creates, so a test can wait on them."""
    created = []
    make_web_service = shared.make_web_service

    def recording_make_web_service(**kwargs: object) -> pulumi.Resource:
        service = make_web_service(**kwargs)  # ty: ignore[invalid-argument-type]
        created.append(service)
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
        assert build_args["VITE_AUTH0_DOMAIN"] == "open-reaction-database.us.auth0.com"

        service = pulumi_mocks.inputs_of("awsx:ecs:FargateService")
        environment = {
            variable["name"]: variable["value"]
            for variable in service["taskDefinitionArgs"]["container"]["environment"]
        }
        # The backend verifies tokens against the same tenant the UI signs in with.
        for name in ("VITE_AUTH0_DOMAIN", "VITE_AUTH0_AUDIENCE", "VITE_AUTH0_ISSUER"):
            assert environment[name] == build_args[name]

    (service,) = services
    return service.urn.apply(check)
