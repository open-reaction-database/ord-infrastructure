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

"""In-database Postgres roles and grants for the RDS cluster.

This stack talks the Postgres wire protocol directly, which the cluster only
allows through the bastion SSM tunnel — so `pulumi preview`/`up` here require the
tunnel open on localhost:15432. See README.md.
"""

import pulumi
import pulumi_aws as aws
import pulumi_postgresql as postgresql

backend = pulumi.StackReference("ord/backend/prod")

# The provider connects to localhost, where the bastion tunnel forwards to RDS.
TUNNEL_HOST = "localhost"
TUNNEL_PORT = 15432

# The read-only role is granted across every application database in the cluster.
DATABASES = ["app", "ord", "editor"]

# Credentials come from the secrets the backend stack manages: the master user to
# connect as, and the generated password the `readonly` role should have.
master_password = aws.secretsmanager.get_secret_version_output(
    secret_id=backend.get_output("rds_password_secret_arn")
).secret_string
readonly_password = aws.secretsmanager.get_secret_version_output(
    secret_id=backend.get_output("rds_ro_password_secret_arn")
).secret_string

# One provider per database: schema/table grants must run while connected to the
# target database. They all reach the cluster through the same tunnel.
providers = {
    db: postgresql.Provider(
        f"pg_{db}",
        host=TUNNEL_HOST,
        port=TUNNEL_PORT,
        username="ord",
        password=master_password,
        database=db,
        sslmode="require",
        # The RDS master user has rds_superuser, not true superuser; tell the
        # provider not to assume superuser behavior (e.g. SET ROLE) it can't do.
        superuser=False,
    )
    for db in DATABASES
}

# The role is a cluster-global object; create it once via any provider.
readonly = postgresql.Role(
    "readonly",
    name="readonly",
    login=True,
    password=readonly_password,
    opts=pulumi.ResourceOptions(provider=providers["ord"]),
)

for db, provider in providers.items():
    opts = pulumi.ResourceOptions(provider=provider, depends_on=[readonly])
    postgresql.Grant(
        f"{db}_connect",
        database=db,
        role=readonly.name,
        object_type="database",
        privileges=["CONNECT"],
        opts=opts,
    )
    postgresql.Grant(
        f"{db}_usage",
        database=db,
        schema="public",
        role=readonly.name,
        object_type="schema",
        privileges=["USAGE"],
        opts=opts,
    )
    postgresql.Grant(
        f"{db}_select",
        database=db,
        schema="public",
        role=readonly.name,
        object_type="table",
        objects=[],  # empty list = all tables currently in the schema
        privileges=["SELECT"],
        opts=opts,
    )
    # Future tables created by the master user are readable too, so loads don't
    # silently leave the readonly role unable to see new tables.
    postgresql.DefaultPrivileges(
        f"{db}_select_future",
        database=db,
        schema="public",
        owner="ord",
        role=readonly.name,
        object_type="table",
        privileges=["SELECT"],
        opts=opts,
    )
