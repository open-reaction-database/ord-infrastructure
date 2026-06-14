# app

Pulumi project for ord-app: ECS Fargate service, ALB, ECR image, and DNS. It has
two stacks:

| Stack | URL | Database | Image source |
|---|---|---|---|
| `ord/prod` | `app.open-reaction-database.org` | `app` | sibling repo on clean `main` |
| `ord/staging` | `app-staging.open-reaction-database.org` | `app_staging` | sibling repo, **any branch** |

Per-environment settings come from stack config (`Pulumi.<stack>.yaml`):
`subdomain`, `database`, and `enforce_clean`. Prod uses the defaults, so it needs
no config; staging overrides all three.

## Database connection

The container gets a **passwordless** `PG_DSN`
(`postgresql+psycopg://ord@<endpoint>:5432/<database>`) as a plain env var and the
password via the `PGPASSWORD` secret (the shared `rds_password`). So switching
environments is just a different database name — no per-environment DSN secret.

## Staging: bring it up / tear it down

Staging is meant to be ephemeral — stand it up to test a change, destroy it when
done. It builds the image from whatever is checked out in `../../../ord-app`
(the `enforce_clean: false` config relaxes the clean-`main` gate that prod
enforces).

```sh
# Bring up (or update) staging from the current ord-app working tree:
pulumi -C stacks/app up      --stack ord/staging

# Tear it down (ALB + ECS + ECR all go; cost returns to ~$0):
pulumi -C stacks/app destroy --stack ord/staging
```

Auth0 already lists `app-staging.open-reaction-database.org` as an allowed
callback/logout URL (see `stacks/auth`), so login works against staging without
further changes. The `app_staging` database is managed by the `database` stack;
its schema is created by running ord-app's Alembic migrations against it.

> Tearing down staging leaves the `app_staging` database (managed by the
> `database` stack) and the Auth0 callback URL in place — only the compute/ALB/DNS
> for staging are removed.
