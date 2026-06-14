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

"""ECS Fargate service, ALB, and DNS for ord-interface."""

import pulumi
import pulumi_aws as aws
import pulumi_awsx as awsx

from ord_infrastructure.shared import make_web_service

backend = pulumi.StackReference("ord/backend/prod")
domain = pulumi.StackReference("ord/domain/prod")

github_client_secret = aws.secretsmanager.Secret("github_client_secret", name="github-client")

make_web_service(
    backend=backend,
    domain=domain,
    container_port=8080,
    certificate_arn=domain.get_output("certificate_arn"),
    record_name=domain.get_output("domain_name"),
    sibling_path="../../../ord-interface",
    dockerfile="../../../ord-interface/ord_interface/Dockerfile",
    secret_arns=[backend.get_output("rds_password_secret_arn"), github_client_secret.arn],
    environment=[
        awsx.ecs.TaskDefinitionKeyValuePairArgs(name="POSTGRES_HOST", value=backend.get_output("rds_endpoint")),
        awsx.ecs.TaskDefinitionKeyValuePairArgs(name="POSTGRES_USER", value="ord"),
        awsx.ecs.TaskDefinitionKeyValuePairArgs(name="POSTGRES_DATABASE", value="ord"),
        awsx.ecs.TaskDefinitionKeyValuePairArgs(name="REDIS_HOST", value=backend.get_output("redis_endpoint")),
        awsx.ecs.TaskDefinitionKeyValuePairArgs(name="REDIS_SSL", value="1"),
    ],
    secrets=[
        awsx.ecs.TaskDefinitionSecretArgs(
            name="POSTGRES_PASSWORD",
            value_from=backend.get_output("rds_password_secret_arn"),
        ),
        awsx.ecs.TaskDefinitionSecretArgs(
            name="GH_CLIENT_ID",
            value_from=github_client_secret.arn.apply(lambda arn: f"{arn}:GH_CLIENT_ID::"),  # ty: ignore[missing-argument, invalid-argument-type]
        ),
        awsx.ecs.TaskDefinitionSecretArgs(
            name="GH_CLIENT_SECRET",
            value_from=github_client_secret.arn.apply(lambda arn: f"{arn}:GH_CLIENT_SECRET::"),  # ty: ignore[missing-argument, invalid-argument-type]
        ),
    ],
)
