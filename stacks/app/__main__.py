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
# This environment's rule on the shared HTTPS listener; unique across services.
listener_rule_priority = config.get_int("listener_rule_priority") or 200
# Prod requires the sibling repo on a clean `main`; staging deploys any branch.
enforce_clean = config.get_bool("enforce_clean")
if enforce_clean is None:
    enforce_clean = True
# Prod's target group gets a generated name; other environments name theirs after the
# subdomain.
name_prefix = None if subdomain == "app" else subdomain
# Fargate task size. A dataset validation holds one core for minutes at a time, so prod's
# 2 vCPU keep a core free for requests. 4 GB is the least memory Fargate pairs with 2 vCPU.
# ord-app streams downloads, and at this size it peaked at 1.9 GiB uploading a
# 50,688-reaction dataset twice, then downloading it in every format while it validated.
# Staging runs smaller.
cpu = config.get_int("cpu") or 2048
memory = config.get_int("memory") or 4096

backend = pulumi.StackReference("ord/backend/prod")
domain = pulumi.StackReference("ord/domain/prod")
auth = pulumi.StackReference("ord/auth/prod")

# Passwordless DSN — the password is injected separately via PGPASSWORD, so the one
# shared rds_password secret works for every environment and only the database name
# differs. (ord-app's own default DSN is likewise passwordless.)
pg_dsn = pulumi.Output.format(
    "postgresql+psycopg://ord@{0}:5432/{1}",
    backend.get_output("rds_endpoint"),
    database,
)

# Auth0 settings, from the auth stack that owns the ORD App client. The UI compiles them
# into its bundle, so they are image build arguments; the backend verifies access tokens
# against the same tenant, so the task gets them too. ord-app's image build fails if any
# build argument is missing. These use require_output: a missing auth output fails the
# deploy, where get_output would return None and name the issuer https://None/.
auth0_domain = auth.require_output("domain")
auth0_settings = {
    "VITE_AUTH0_DOMAIN": auth0_domain,
    "VITE_AUTH0_CLIENT_ID": auth.require_output("ord_app_client_id"),
    "VITE_AUTH0_AUDIENCE": pulumi.Output.format("https://{0}/api/v2/", auth0_domain),
    "VITE_AUTH0_ISSUER": pulumi.Output.format("https://{0}/", auth0_domain),
}

domain_name = domain.get_output("domain_name")
record_name = domain_name.apply(lambda name: f"{subdomain}.{name}")  # ty: ignore[missing-argument, invalid-argument-type]

make_web_service(
    backend=backend,
    domain=domain,
    container_port=5173,
    record_name=record_name,
    listener_rule_priority=listener_rule_priority,
    # Served by uvicorn through nginx's /api/v1/ proxy, with no login or database.
    health_check_path="/api/v1/canonicalize-smiles?smiles=C",
    sibling_path="../../../ord-app",
    dockerfile="../../../ord-app/Dockerfile.single",
    secret_arns=[backend.get_output("rds_password_secret_arn")],
    environment=[
        awsx.ecs.TaskDefinitionKeyValuePairArgs(name="PG_DSN", value=pg_dsn),
        *(
            awsx.ecs.TaskDefinitionKeyValuePairArgs(name=name, value=value)
            for name, value in auth0_settings.items()
        ),
    ],
    build_args={
        **auth0_settings,
        "VITE_AUTH0_SCOPE": "openid profile email offline_access",
    },
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
