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

"""ECS Fargate service, ALB, and DNS for ord-app.

Per-environment knobs come from stack config; the defaults are the prod values, so
the prod stack needs no config. The `staging` stack overrides them (see
Pulumi.staging.yaml) to serve app-staging.open-reaction-database.org from the
app_staging database, built from whatever branch is checked out.
"""

import pulumi
import pulumi_awsx as awsx

from ord_infrastructure.shared import make_web_service

config = pulumi.Config()
subdomain = config.get("subdomain") or "app"
database = config.get("database") or "app"
# Prod requires the sibling repo on a clean `main`; staging deploys any branch.
enforce_clean = config.get_bool("enforce_clean")
if enforce_clean is None:
    enforce_clean = True
# Prod keeps its existing auto-generated ALB/target-group names (name_prefix=None);
# new environments need an explicit prefix (AWS forbids underscores in those names).
name_prefix = None if subdomain == "app" else subdomain
# Fargate task size — prod's default is 4 vCPU / 8 GB; staging runs smaller/cheaper.
cpu = config.get_int("cpu") or 4096
memory = config.get_int("memory") or 8192

backend = pulumi.StackReference("ord/backend/prod")
domain = pulumi.StackReference("ord/domain/prod")

# Passwordless DSN — the password is injected separately via PGPASSWORD, so the one
# shared rds_password secret works for every environment and only the database name
# differs. (ord-app's own default DSN is likewise passwordless.)
pg_dsn = pulumi.Output.format(
    "postgresql+psycopg://ord@{0}:5432/{1}",
    backend.get_output("rds_endpoint"),
    database,
)

domain_name = domain.get_output("domain_name")
record_name = domain_name.apply(lambda name: f"{subdomain}.{name}")  # ty: ignore[missing-argument, invalid-argument-type]

make_web_service(
    backend=backend,
    domain=domain,
    container_port=5173,
    certificate_arn=domain.get_output("wildcard_certificate_arn"),
    record_name=record_name,
    sibling_path="../../../ord-app",
    dockerfile="../../../ord-app/Dockerfile.single",
    secret_arns=[backend.get_output("rds_password_secret_arn")],
    environment=[
        awsx.ecs.TaskDefinitionKeyValuePairArgs(name="PG_DSN", value=pg_dsn),
    ],
    secrets=[
        awsx.ecs.TaskDefinitionSecretArgs(
            name="PGPASSWORD",
            value_from=backend.get_output("rds_password_secret_arn"),
        ),
    ],
    enforce_clean=enforce_clean,
    name_prefix=name_prefix,
    cpu=cpu,
    memory=memory,
    cluster_name=subdomain,  # "app" (prod) / "app-staging" — distinguishable in the console
)
