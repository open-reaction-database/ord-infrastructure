# app

Pulumi project for ord-app: ECS Fargate service, a rule on the backend's shared
load balancer, ECR image, and DNS. It has two stacks:

| Stack | URL | Database | Image source |
|---|---|---|---|
| `ord/prod` | `app.open-reaction-database.org` | `app` | sibling repo on clean `main` |
| `ord/staging` | `app-staging.open-reaction-database.org` | `app` (prod's) | sibling repo, **any branch** |

Per-environment settings come from stack config (`Pulumi.<stack>.yaml`):
`subdomain`, `database`, and `enforce_clean`. Prod uses the defaults, so it needs
no config; staging overrides `subdomain` and `enforce_clean` and keeps prod's
`database` and task size (`cpu`, `memory`).

The Auth0 tenant domain and the ORD App client ID come from the
[`auth` stack](../auth/README.md), which owns that client. The image build receives
them as build arguments, which the UI compiles into its bundle, and the task receives
them as environment for the backend's token checks. A checkout of ord-app therefore
needs no `.env` files to build a working image.

## Database connection

The container gets a **passwordless** `PG_DSN`
(`postgresql+psycopg://ord@<endpoint>:5432/<database>`) as a plain env var and the
password via the `PGPASSWORD` secret (the shared `rds_password`). So switching
environments is just a different database name — no per-environment DSN secret.

## Download link key

ord-app signs its short-lived download links with `DOWNLOAD_LINK_SECRET`, and refuses
to start without it. The stack generates a key per environment, stores it in Secrets
Manager (`download_link_secret`), and injects it into the task as a secret. ECS reads
the secret when a task starts, so a replaced key reaches the service only as its tasks
restart, for example on the next deploy; it invalidates links made in the 30 seconds
before that.

## Staging: bring it up / tear it down

Staging is meant to be ephemeral — stand it up to test a change, destroy it when
done. It builds the image from whatever is checked out in `../../../ord-app`
(the `enforce_clean: false` config relaxes the clean-`main` gate that prod
enforces).

```sh
# Bring up (or update) staging from the current ord-app working tree:
pulumi -C stacks/app up      --stack ord/staging

# Tear it down (listener rule + ECS + ECR all go; cost returns to ~$0):
pulumi -C stacks/app destroy --stack ord/staging
```

Auth0 already lists `app-staging.open-reaction-database.org` as an allowed
callback/logout URL (see `stacks/auth`), so login works against staging without
further changes. Staging uses prod's `app` database, so it reads and writes prod's
data. A branch that adds an Alembic migration needs that migration applied to `app`
before it runs on staging.

> Tearing down staging leaves the Auth0 callback URL in place — only the compute, the
> listener rule, and DNS for staging are removed.
