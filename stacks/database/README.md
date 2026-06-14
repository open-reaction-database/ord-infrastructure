# database

Pulumi stack for **in-database** objects on the RDS cluster — currently the
`readonly` Postgres role and its grants. It uses the
[`postgresql`](https://www.pulumi.com/registry/packages/postgresql/) provider,
which speaks the Postgres wire protocol directly.

## Why this is a separate stack

The cluster has no public endpoint, so the provider can only reach it through the
bastion SSM tunnel. Keeping these resources out of `backend` means a normal
`backend` deploy (VPC, RDS, etc.) stays self-contained and tunnel-free; the tunnel
dependency is quarantined here, in the one stack that actually needs it.

## What it manages

- The application **databases**: `app`, `ord`, and `editor` (imported and
  `protect`ed — they predate IaC and hold data, so they're adopted in place, never
  recreated) plus `app_staging` (created here, for the staging app).
- The **`readonly`** role (LOGIN), password sourced from the `rds_ro_password`
  secret that `backend` owns.
- `CONNECT` + `USAGE` + `SELECT` on `public`, plus default privileges for future
  tables, across all four databases.

## Deploying

This stack **requires the bastion tunnel open on `localhost:15432`** — the
provider connects on both `preview` and `up`. Deploy `backend` first (it creates
the `rds_ro_password` secret this stack reads).

```sh
# 1. Open the tunnel (leave it running in another terminal) — see ../backend/README.md:
BASTION=$(pulumi -C stacks/backend stack output bastion_instance_id)
RDS=$(pulumi -C stacks/backend stack output rds_endpoint)
aws ssm start-session \
  --target "$BASTION" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$RDS\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"15432\"]}"

# 2. With the tunnel up:
pulumi -C stacks/database preview --stack ord/prod
pulumi -C stacks/database up      --stack ord/prod
```

If `preview`/`up` hangs or errors with a connection failure, the tunnel isn't up
on `localhost:15432`.

## Adding a database

New application databases aren't created here (the cluster auto-creates `ord`;
others are created out-of-band). To extend the `readonly` grants to a new
database, add its name to `DATABASES` in `__main__.py` and re-run `up` with the
tunnel open.
