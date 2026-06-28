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

github_client_secret = aws.secretsmanager.Secret(
    "github_client_secret", name="github-client"
)
gh_arn = github_client_secret.arn
gh_client_id = gh_arn.apply(lambda arn: f"{arn}:GH_CLIENT_ID::")  # ty: ignore[missing-argument, invalid-argument-type]
gh_client_secret = gh_arn.apply(lambda arn: f"{arn}:GH_CLIENT_SECRET::")  # ty: ignore[missing-argument, invalid-argument-type]

# Anthropic API key for the natural-language search endpoint, named per-service so other
# services can have their own keys later. The value comes from an encrypted Pulumi config
# secret (`pulumi config set --secret anthropic_api_key ...`), so a single `pulumi up`
# brings the service up with a working key -- no out-of-band bootstrap. Rotating the key
# (config set + up) still needs `aws ecs update-service --force-new-deployment` to take
# effect, since ECS only resolves `secrets` at task startup.
config = pulumi.Config()
anthropic_api_key = config.require_secret("anthropic_api_key")
anthropic_api_key_secret = aws.secretsmanager.Secret(
    "anthropic_api_key_secret", name="ord-interface-anthropic-api-key"
)
aws.secretsmanager.SecretVersion(
    "anthropic_api_key_version",
    secret_id=anthropic_api_key_secret.id,
    secret_string=anthropic_api_key,
)

make_web_service(
    backend=backend,
    domain=domain,
    container_port=8080,
    certificate_arn=domain.get_output("certificate_arn"),
    record_name=domain.get_output("domain_name"),
    sibling_path="../../../ord-interface",
    dockerfile="../../../ord-interface/ord_interface/Dockerfile",
    secret_arns=[
        backend.get_output("rds_password_secret_arn"),
        github_client_secret.arn,
        anthropic_api_key_secret.arn,
    ],
    environment=[
        awsx.ecs.TaskDefinitionKeyValuePairArgs(
            name="POSTGRES_HOST", value=backend.get_output("rds_endpoint")
        ),
        awsx.ecs.TaskDefinitionKeyValuePairArgs(name="POSTGRES_USER", value="ord"),
        awsx.ecs.TaskDefinitionKeyValuePairArgs(name="POSTGRES_DATABASE", value="ord"),
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
            name="GH_CLIENT_ID",
            value_from=gh_client_id,
        ),
        awsx.ecs.TaskDefinitionSecretArgs(
            name="GH_CLIENT_SECRET",
            value_from=gh_client_secret,
        ),
        awsx.ecs.TaskDefinitionSecretArgs(
            name="ANTHROPIC_API_KEY",
            value_from=anthropic_api_key_secret.arn,
        ),
    ],
    cluster_name="interface",
)
