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

"""Pulumi mocks shared by the tests that build resources."""

from typing import ClassVar

import pulumi
import pytest


class RecordingMocks(pulumi.runtime.Mocks):
    """Records the inputs of every resource registered, without creating any."""

    # The outputs the app stack and make_web_service read, per stack, so a value read
    # from the wrong stack comes back missing.
    STACK_OUTPUTS: ClassVar[dict[str, dict[str, object]]] = {
        "ord/backend/prod": {
            "vpc_id": "vpc-0",
            "vpc_cidr_block": "10.0.0.0/16",
            "private_subnet_ids": ["subnet-0", "subnet-1"],
            "https_listener_arn": "arn:aws:elasticloadbalancing:listener/0",
            "load_balancer_dns_name": "lb.example.com",
            "load_balancer_zone_id": "Z0",
            "rds_endpoint": "db.example.com",
            "rds_password_secret_arn": "arn:aws:secretsmanager:secret/0",
        },
        "ord/domain/prod": {
            "zone_id": "Z1",
            "domain_name": "example.com",
        },
        "ord/auth/prod": {
            "domain": "tenant.auth0.example.com",
            "ord_app_client_id": "client-0",
        },
    }

    def __init__(self) -> None:
        self.resources: list[pulumi.runtime.MockResourceArgs] = []

    def new_resource(
        self, args: pulumi.runtime.MockResourceArgs
    ) -> tuple[str | None, dict]:
        self.resources.append(args)
        if args.typ == "pulumi:pulumi:StackReference":
            outputs = self.STACK_OUTPUTS[args.name]
            return f"{args.name}_id", {"name": args.name, "outputs": outputs}
        # Outputs that the provider computes and a stack reads back.
        computed = {
            "aws:secretsmanager/secret:Secret": {
                "arn": f"arn:aws:secretsmanager:secret/{args.name}"
            },
            "random:index/randomPassword:RandomPassword": {"result": "generated"},
        }
        return f"{args.name}_id", dict(args.inputs) | computed.get(args.typ, {})

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict, list | None]:
        return {}, None

    def inputs_of(self, typ: str) -> dict:
        """Return the inputs of the one resource of type `typ`."""
        (resource,) = [r for r in self.resources if r.typ == typ]
        return resource.inputs


@pytest.fixture
def pulumi_mocks() -> RecordingMocks:
    mocks = RecordingMocks()
    # Named like the app stack, so its program reads its own config namespace.
    pulumi.runtime.set_mocks(mocks, project="app", stack="prod", preview=False)
    return mocks
