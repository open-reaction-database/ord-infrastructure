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

"""ECS Fargate service, ALB, and DNS for the ORD viewer.

Each task fetches the corpus it serves from Hugging Face when it starts: the derived
artifacts from open-reaction-database/ord-artifacts and their source datasets from the
open-reaction-database/ord-data mirror, at the revisions below.
"""

import pulumi
import pulumi_aws as aws
import pulumi_awsx as awsx

from ord_infrastructure.shared import make_web_service

# The corpus the viewer serves, as commits so every task fetches the same one. The
# artifacts revision must hold a build that ord-app's pinned ord-schema and RDKit read,
# and the sources revision must mirror the ord-data commit that build names. Changing
# them is what promotes a new build to serve traffic.
ARTIFACTS_REVISION = "6662a8bc7c61a01e3b8782cc50cb6d11e217bfa8"
SOURCES_REVISION = "46980f8cb68aa43ed9707edec319d4eac5a2bfed"

# Under /app, which the image's user owns.
CORPUS_DIR = "/app/corpus"

# Fargate task size. The process holds the opened corpus in memory, with room for the
# searches that run over it; 16 GiB is the most Fargate pairs with 2 vCPU.
CPU = 2048
MEMORY = 16384
# Task memory, in MiB, kept from DuckDB for the rest of the process: Python, RDKit, and
# nginx. DuckDB gets the remainder as its memory_limit and spills to disk past it, so it
# cannot take the memory the rest of the process needs.
RESERVED_MEMORY = 6144

# The image's layers, the fetched corpus, and whatever DuckDB spills share this storage.
EPHEMERAL_STORAGE_GIB = 50

# A task fetches and opens its corpus, which takes minutes, before its API answers the
# health check. ECS ignores failed checks for this long after a task starts, so a task
# still starting is not replaced; one that fails to open its corpus exits on its own.
HEALTH_CHECK_GRACE_PERIOD_SECONDS = 30 * 60

backend = pulumi.StackReference("ord/backend/prod")
domain = pulumi.StackReference("ord/domain/prod")

# The key /api/ask calls the model with, an encrypted Pulumi config secret. Without it
# the stack makes no secret, and /api/ask answers 503 while every other route works.
config = pulumi.Config()
anthropic_api_key = config.get_secret("anthropic_api_key")
secret_arns = []
secrets = []
depends_on = []
if anthropic_api_key is not None:
    # Named by prefix: a deleted secret holds its name through its recovery window, so
    # a fixed name would block creating the secret again.
    anthropic_api_key_secret = aws.secretsmanager.Secret(
        "anthropic_api_key_secret", name_prefix="ord-viewer-anthropic-api-key-"
    )
    anthropic_api_key_version = aws.secretsmanager.SecretVersion(
        "anthropic_api_key_version",
        secret_id=anthropic_api_key_secret.id,
        secret_string=anthropic_api_key,
    )
    secret_arns.append(anthropic_api_key_secret.arn)
    secrets.append(
        awsx.ecs.TaskDefinitionSecretArgs(
            name="ORD_ANTHROPIC_API_KEY", value_from=anthropic_api_key_secret.arn
        )
    )
    # A task resolves its secrets when it starts, so the secret needs a value first.
    depends_on.append(anthropic_api_key_version)

environment = {
    "ORD_VIEWER_CORPUS_DIR": CORPUS_DIR,
    "ORD_VIEWER_ARTIFACTS_REVISION": ARTIFACTS_REVISION,
    "ORD_VIEWER_SOURCES_REVISION": SOURCES_REVISION,
    "ORD_VIEWER_MEMORY_LIMIT": f"{MEMORY - RESERVED_MEMORY}MiB",
}

make_web_service(
    backend=backend,
    domain=domain,
    container_port=8080,
    record_name=domain.get_output("domain_name"),
    # This service's rule on the shared HTTPS listener; unique across services.
    listener_rule_priority=100,
    # Served by the API through nginx's /api/ proxy once it has opened the corpus.
    health_check_path="/api/health",
    sibling_path="../../../ord-app",
    dockerfile="../../../ord-app/deploy/viewer/Dockerfile",
    secret_arns=secret_arns,
    environment=[
        awsx.ecs.TaskDefinitionKeyValuePairArgs(name=name, value=value)
        for name, value in environment.items()
    ],
    secrets=secrets,
    cpu=CPU,
    memory=MEMORY,
    cluster_name="viewer",
    depends_on=depends_on,
    health_check_grace_period_seconds=HEALTH_CHECK_GRACE_PERIOD_SECONDS,
    ephemeral_storage_gib=EPHEMERAL_STORAGE_GIB,
)
