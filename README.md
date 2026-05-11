# ord-infrastructure

Pulumi stacks that provision the AWS infrastructure for [ord-app](https://github.com/open-reaction-database/ord-app).

## Layout

```
ord-infrastructure/
├── stacks/                  # One subdirectory per Pulumi project
│   ├── account/             # IAM, AWS Identity Center (SSO), account-level S3 BPA
│   ├── domain/              # Route 53 hosted zone and ACM certificates
│   ├── backend/             # VPC, RDS Aurora, Redis, bastion (see backend/README.md)
│   ├── app/                 # ECS service, ALB, task definitions for ord-app
│   └── interface/           # ECS service, ALB, task definitions for ord-interface
├── ord_infrastructure/      # Installable Python package of helpers shared across stacks
│   └── shared.py            # assert_sibling_clean, make_ecs_execution_role
└── pyproject.toml           # Build config for ord_infrastructure + tool config (ruff, ty)
```

Each project has its own stack (`ord/<project>/prod`) and is deployed independently. Stack-to-stack references go through Pulumi `StackReference` (e.g. `app` reads outputs from `backend` and `domain`).

## Prerequisites

- [Pulumi CLI](https://www.pulumi.com/docs/install/) (3.220+)
- AWS CLI configured for SSO
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) for tool-env management

## One-time setup

```sh
aws sso login
pulumi login
```

SSO credentials expire periodically — re-run `aws sso login` when Pulumi reports `Failed to refresh cached SSO credentials`.

Each Pulumi project's `Pulumi.yaml` declares `runtime.options.virtualenv: venv`, so Pulumi auto-creates a per-project venv and installs `requirements.txt` (which is just `-e ../..`) on the first run. No manual venv setup needed.

For lint/typecheck tools (ruff, ty) at the repo root:

```sh
uv sync --locked   # installs ord_infrastructure + dev deps into .venv
```

## Deploying

From any project directory:

```sh
pulumi preview --stack ord/prod   # show planned changes
pulumi up      --stack ord/prod   # apply
```

Always run `preview` first and review the diff. The `prod` stack is the only stack that exists today — there is no separate dev/staging.

### Building images from sibling repos

`stacks/app` and `stacks/interface` build their docker images from the sibling repos at `~/github/ord/ord-app` and `~/github/ord/ord-interface`. Before the build, [`assert_sibling_clean`](ord_infrastructure/shared.py) checks that the sibling is on `main` and up to date with `origin/main` — `pulumi up` will fail loudly if it isn't.

To bypass the check for a local hotfix:

```sh
PULUMI_ALLOW_DIRTY=1 pulumi up --stack ord/prod
```

## Inspecting state

```sh
pulumi stack ls
pulumi stack output --stack ord/prod
pulumi stack output --stack ord/prod --show-secrets <name>
```

The Pulumi Cloud console is at <https://app.pulumi.com/ord>.

## Recommended deploy order

When bringing the stacks up from scratch:

1. `account` — IAM, SSO, account-level controls
2. `domain` — DNS + certs
3. `backend` — VPC, database, cache (other stacks depend on its outputs via stack references)
4. `app`
5. `interface`

For routine changes, deploy only the project you modified.

## Checks

`pre-commit` and CI (`.github/workflows/checks.yml`) run [addlicense](https://github.com/google/addlicense), ruff (check + format), and [ty](https://github.com/astral-sh/ty). Install the pre-commit hook locally:

```sh
pre-commit install
```
