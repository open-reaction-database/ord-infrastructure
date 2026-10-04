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

# Anthropic API key for the natural-language search endpoint, named per-service so other
# services can have their own keys. The value is an encrypted Pulumi config secret.
config = pulumi.Config()
anthropic_api_key = config.require_secret("anthropic_api_key")
anthropic_api_key_secret = aws.secretsmanager.Secret(
    "anthropic_api_key_secret", name="ord-interface-anthropic-api-key"
)
anthropic_api_key_version = aws.secretsmanager.SecretVersion(
    "anthropic_api_key_version",
    secret_id=anthropic_api_key_secret.id,
    secret_string=anthropic_api_key,
)

make_web_service(
    backend=backend,
    domain=domain,
    container_port=8080,
    record_name=domain.get_output("domain_name"),
    listener_rule_priority=100,
    sibling_path="../../../ord-interface",
    dockerfile="../../../ord-interface/ord_interface/Dockerfile",
    secret_arns=[
        backend.get_output("rds_password_secret_arn"),
        anthropic_api_key_secret.arn,
    ],
    environment=[
        awsx.ecs.TaskDefinitionKeyValuePairArgs(
            name="POSTGRES_HOST", value=backend.get_output("rds_endpoint")
        ),
        awsx.ecs.TaskDefinitionKeyValuePairArgs(name="POSTGRES_USER", value="ord"),
        # The search database the API reads. Each ord-schema load lands in a fresh
        # `ord_<date>` database; pointing here is what promotes one to serve traffic.
        awsx.ecs.TaskDefinitionKeyValuePairArgs(
            name="POSTGRES_DATABASE", value="ord_20260702"
        ),
        awsx.ecs.TaskDefinitionKeyValuePairArgs(
            name="REDIS_HOST", value=backend.get_output("redis_endpoint")
        ),
        awsx.ecs.TaskDefinitionKeyValuePairArgs(name="REDIS_SSL", value="1"),
    ],
    secrets=[
        awsx.ecs.TaskDefinitionSecretArgs(
            name="POSTGRES_PASSWORD",
            value_from=backend.get_output("rds_password_secret_arn"),
        ),
        awsx.ecs.TaskDefinitionSecretArgs(
            name="ANTHROPIC_API_KEY",
            value_from=anthropic_api_key_secret.arn,
        ),
    ],
    cluster_name="interface",
    # Create the secret's first version before the service, so a fresh deploy never
    # starts a task against a versionless secret (which would fail to resolve).
    depends_on=[anthropic_api_key_version],
)
