# Copyright 2025 Open Reaction Database Project Authors
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

"""ECS Fargate service, ALB, and DNS for ord-app."""

import pulumi
import pulumi_awsx as awsx

from ord_infrastructure.shared import make_web_service

backend = pulumi.StackReference("ord/backend/prod")
domain = pulumi.StackReference("ord/domain/prod")

make_web_service(
    backend=backend,
    domain=domain,
    container_port=5173,
    certificate_arn=domain.get_output("wildcard_certificate_arn"),
    record_name=domain.get_output("domain_name").apply(lambda name: f"app.{name}"),  # ty: ignore[missing-argument, invalid-argument-type]
    sibling_path="../../../ord-app",
    dockerfile="../../../ord-app/Dockerfile.single",
    secret_arns=[backend.get_output("rds_dsn_secret_arn")],
    secrets=[
        awsx.ecs.TaskDefinitionSecretArgs(
            name="PG_DSN",
            value_from=backend.get_output("rds_dsn_secret_arn"),
        ),
    ],
)
