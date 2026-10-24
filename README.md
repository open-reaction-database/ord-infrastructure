# ord-infrastructure

Pulumi stacks that provision the AWS infrastructure for [ord-app](https://github.com/open-reaction-database/ord-app).

## Layout

```
ord-infrastructure/
├── stacks/                  # One subdirectory per Pulumi project
│   ├── account/             # IAM, AWS Identity Center (SSO), account-level S3 BPA, deploy sign-in
│   ├── domain/              # Route 53 hosted zone and ACM certificates
│   ├── backend/             # VPC, RDS Aurora, bastion, logs bucket (see backend/README.md)
│   ├── database/            # In-DB Postgres roles/grants via the bastion tunnel (see database/README.md)
│   ├── auth/                # Auth0 resources: the ORD App client (see auth/README.md)
│   ├── editor/              # ECS service, listener rule, task definitions for ord-app's editor
│   └── viewer/              # ECS service, listener rule, task definitions for ord-app's viewer
├── ord_infrastructure/      # Installable Python package of helpers shared across stacks
│   └── shared.py            # assert_sibling_clean, make_web_service, make_ecs_execution_role
└── pyproject.toml           # Build config for ord_infrastructure + tool config (ruff, ty)
```

Each project has its own stack (`ord/<project>/prod`) and is deployed independently. Stack-to-stack references go through Pulumi `StackReference` (e.g. `editor` reads outputs from `backend` and `domain`).

## Prerequisites

- [Pulumi CLI](https://www.pulumi.com/docs/install/) (3.220+)
- AWS CLI configured for SSO
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) for tool-env management
- Docker, to build the `editor` and `viewer` images, and [`jq`](https://jqlang.org/) to remove their builder afterward (see [Removing the build container](#removing-the-build-container))

## One-time setup

```sh
aws sso login
pulumi login
```

SSO credentials expire periodically — re-run `aws sso login` when Pulumi reports `Failed to refresh cached SSO credentials`.

Each Pulumi project's `Pulumi.yaml` declares `runtime.options.virtualenv: venv`, so Pulumi auto-creates a per-project venv and installs `requirements.txt` (which is just `-e ../..`) on the first run. No manual venv setup needed.

For lint/typecheck/test tools (ruff, ty, pytest) at the repo root:

```sh
uv sync --locked   # installs ord_infrastructure + dev deps into .venv
```

## Tests

```sh
uv run pytest
```

The suite covers the deploy-time guards in [`ord_infrastructure/shared.py`](ord_infrastructure/shared.py) — the sibling-repo cleanliness check, the image provenance stamp, and the target-group name-length limit — against throwaway git repositories. Under Pulumi mocks ([`conftest.py`](ord_infrastructure/conftest.py)), it checks what `make_web_service` builds, and [`stacks_test.py`](ord_infrastructure/stacks_test.py) runs the `editor` and `viewer` programs and checks what they pass it. It needs no AWS credentials and no Pulumi runtime.

The mocks create nothing, so `pulumi preview` is still what validates a change against AWS. Review the preview diff before every `up`.

## Deploying

From any project directory:

```sh
pulumi preview --stack ord/prod   # show planned changes
pulumi up      --stack ord/prod   # apply
```

Always run `preview` first and review the diff. Every project has a `prod` stack, and `stacks/editor` also has a `staging` stack; see [`stacks/editor/README.md`](stacks/editor/README.md).

### From GitHub Actions

Two workflows deploy the web services. Neither runs on its own: start one from the repository's Actions tab, or with `gh workflow run`.

| Workflow | Stack | Builds ord-app from |
|---|---|---|
| Deploy the editor ([`deploy_editor.yml`](.github/workflows/deploy_editor.yml)) | `stacks/editor`, `prod` or `staging` | `ord_app_ref`, `main` by default; prod accepts only `main` |
| Deploy the viewer ([`deploy_viewer.yml`](.github/workflows/deploy_viewer.yml)) | `stacks/viewer`, `prod` | `main` |

Each runs `preview` unless you choose `up`, so the routine is the same as from a terminal: run `preview`, read the diff in the run's log, then run `up`. A run checks out ord-app beside this repository, installs the stack's dependencies at the versions in `uv.lock`, builds the image on an arm64 runner, and signs in through GitHub's OIDC provider: to AWS as the account stack's deploy role, and to Pulumi Cloud with an `ord` organization token. No AWS key or Pulumi token is stored in GitHub. A viewer run also checks, before it signs in, that the viewer can start on the corpus its stack pins (see [`stacks/viewer/README.md`](stacks/viewer/README.md)); an editor run has no such check.

One-time setup:

1. `pulumi up` the [`account`](stacks/account/README.md) stack, which creates the deploy role, exports its ARN as `deploy_role_arn`, and registers GitHub as an OIDC issuer in the `ord` Pulumi organization. Registering the issuer takes an organization admin, and the organization tokens it grants need a Pulumi Cloud plan that offers them.
2. In the repository's settings, under **Secrets and variables → Actions → Variables**, set `AWS_DEPLOY_ROLE_ARN` to that ARN.
3. Under **Environments**, create `prod` and `staging`, and in each, limit **Deployment branches** to `main`. The deploy role and the Pulumi issuer trust any job in these two environments, so the branch limit is what keeps another branch from deploying. To make each run wait for approval, add required reviewers to the environment.

### Building images from sibling repos

`stacks/editor` and `stacks/viewer` build their docker images from the sibling checkout `../ord-app`, next to this repository. Before the build, [`assert_sibling_clean`](ord_infrastructure/shared.py) checks that the sibling is on `main` and up to date with `origin/main` — `pulumi up` will fail loudly if it isn't.

To bypass the check for a local hotfix:

```sh
PULUMI_ALLOW_DIRTY=1 pulumi up --stack ord/prod
```

### Removing the build container

Pulumi's docker-build provider builds the images in a buildx builder, and when no `docker-container` builder is running it creates one with a random name. It never removes that builder ([pulumi/pulumi-docker-build#55](https://github.com/pulumi/pulumi-docker-build/issues/55)), and buildx starts its container again whenever Docker starts, so the container keeps Docker Desktop out of Resource Saver mode. A stopped builder is not reused: the next build creates another, with its own cache volume. After a `preview` or `up` of `editor` or `viewer` from a terminal, remove the builders and their volumes:

```sh
docker buildx ls --format json | jq -r 'select(.Driver == "docker-container") | .Name' | xargs -r -n1 docker buildx rm
docker volume ls --quiet --filter name=buildx_buildkit_ | xargs -r docker volume rm
```

The first line removes every `docker-container` builder, including any you created for other work, and stops any build running in one. The second removes the volumes of builders whose containers were already gone, which `buildx rm` leaves behind. The next build starts with an empty cache. A GitHub Actions runner is discarded after its run, so a workflow deploy leaves nothing to remove.

## Inspecting state

```sh
pulumi stack ls
pulumi stack output --stack ord/prod
pulumi stack output --stack ord/prod --show-secrets <name>
```

The Pulumi Cloud console is at <https://app.pulumi.com/ord>.

### Container logs

The `stacks/editor` and `stacks/viewer` tasks send their containers' stdout and stderr to a CloudWatch log group that awsx creates, named `service-` plus a generated suffix (the task definition's `awslogs-group` option). CloudWatch deletes each event once it is older than the `log_retention_days` the stack passes to [`make_web_service`](ord_infrastructure/shared.py). To follow one:

```sh
aws logs tail <log group> --follow
```

## Recommended deploy order

When bringing the stacks up from scratch:

1. `account` — IAM, SSO, account-level controls
2. `domain` — DNS + certs
3. `backend` — VPC, database (other stacks depend on its outputs via stack references)
4. `database` — in-DB roles/grants; requires the bastion tunnel open (see `database/README.md`)
5. `auth` — the Auth0 client `editor` builds against
6. `editor`
7. `viewer`

For routine changes, deploy only the project you modified.

## Checks

`pre-commit` and CI (`.github/workflows/checks.yml`) run [addlicense](https://github.com/google/addlicense), ruff (check + format), and [ty](https://github.com/astral-sh/ty). Install the pre-commit hook locally:

```sh
pre-commit install
```
