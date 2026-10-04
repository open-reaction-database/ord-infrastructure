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

# Every application database in the cluster. The databases holding real data are
# protected; app_staging is the disposable staging database. The read-only role is
# granted across all of them. `ord_20260702` is the search database built by the
# ord-schema 0.8 ORM pipeline, named for the date its load completed.
DATABASES = ["app", "ord_20260702", "editor", "app_staging"]
PROD_DATABASES = {"app", "ord_20260702", "editor"}

# Databases that already exist on the cluster and must be adopted in place rather than
# created — a create would fail against the live database, and a replace would drop it.
# Add a database here to adopt it, then drop it once `up` has recorded it in state.
IMPORT_DATABASES = set()

# Every database exposes readable tables in public; the readonly role is granted
# there for all of them. The search database also keeps tables in non-public
# schemas, so the role needs USAGE + SELECT on those too: `ord` (ord-schema's ORM
# tables), `rdkit` (the cartridge), and `derived` (generated SMILES and RDKit links).
# The Alembic-managed app databases (app, app_staging) and the editor database use
# public only.
EXTRA_READONLY_SCHEMAS = {
    "ord_20260702": ["ord", "rdkit", "derived"],
}

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

# CREATE DATABASE and database-level settings run against a maintenance database
# that always exists (`postgres`), not the database being managed.
maintenance_provider = postgresql.Provider(
    "pg_maintenance",
    host=TUNNEL_HOST,
    port=TUNNEL_PORT,
    username="ord",
    password=master_password,
    database="postgres",
    sslmode="require",
    superuser=False,
)

# Manage every application database. The pre-existing ones are imported and
# protected so Pulumi adopts them in place without recreating (a replace would drop
# the data); app_staging is created fresh.
databases = {
    db: postgresql.Database(
        f"db_{db}",
        name=db,
        owner="ord",
        opts=pulumi.ResourceOptions(
            provider=maintenance_provider,
            # Existing databases hold real data — protect them. app_staging is
            # disposable test data, so leave it unprotected: retiring staging is
            # then just removing it here + `pulumi up`, with no unprotect step.
            protect=db in PROD_DATABASES,
            # A database's import id is its name.
            import_=db if db in IMPORT_DATABASES else None,
        ),
    )
    for db in DATABASES
}

# A role belongs to the cluster rather than to one database, so it is created
# through the maintenance database.
readonly = postgresql.Role(
    "readonly",
    name="readonly",
    login=True,
    password=readonly_password,
    opts=pulumi.ResourceOptions(provider=maintenance_provider),
)


def grant_schema_read(name_prefix: str, db: str, schema: str, opts) -> None:
    """Grants the readonly role USAGE + SELECT (current and future tables) on a schema.

    Args:
        name_prefix: Prefix for the Pulumi resource names. `public` uses the bare
            database name to preserve existing resource URNs; other schemas qualify
            it with the schema name.
        db: Database the grants apply to.
        schema: Schema to grant on.
        opts: Resource options carrying the database's provider and dependencies.
    """
    postgresql.Grant(
        f"{name_prefix}_usage",
        database=db,
        schema=schema,
        role=readonly.name,
        object_type="schema",
        privileges=["USAGE"],
        opts=opts,
    )
    postgresql.Grant(
        f"{name_prefix}_select",
        database=db,
        schema=schema,
        role=readonly.name,
        object_type="table",
        objects=[],  # empty list = all tables currently in the schema
        privileges=["SELECT"],
        opts=opts,
    )
    # Future tables created by the master user are readable too, so loads don't
    # silently leave the readonly role unable to see new tables.
    postgresql.DefaultPrivileges(
        f"{name_prefix}_select_future",
        database=db,
        schema=schema,
        owner="ord",
        role=readonly.name,
        object_type="table",
        privileges=["SELECT"],
        opts=opts,
    )


for db, provider in providers.items():
    opts = pulumi.ResourceOptions(
        provider=provider, depends_on=[readonly, databases[db]]
    )
    postgresql.Grant(
        f"{db}_connect",
        database=db,
        role=readonly.name,
        object_type="database",
        privileges=["CONNECT"],
        opts=opts,
    )
    grant_schema_read(db, db, "public", opts)
    for schema in EXTRA_READONLY_SCHEMAS.get(db, []):
        grant_schema_read(f"{db}_{schema}", db, schema, opts)
