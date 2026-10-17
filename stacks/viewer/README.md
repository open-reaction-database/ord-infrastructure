# viewer

Pulumi project for the ORD viewer, ord-app's public search and browse site: ECS Fargate service, a rule on the backend's shared load balancer, ECR image, and DNS. It serves `viewer.open-reaction-database.org` from one stack, `ord/prod`, built from ord-app's `deploy/viewer/Dockerfile` on a clean `main`.

## Corpus

The viewer needs no database. Each task fetches its corpus from Hugging Face when it starts, into `/app/corpus` on the task's ephemeral storage:

- the derived artifacts from [ord-artifacts](https://huggingface.co/datasets/open-reaction-database/ord-artifacts) at `ARTIFACTS_REVISION`, and
- their source datasets from the [ord-data mirror](https://huggingface.co/datasets/open-reaction-database/ord-data) at `SOURCES_REVISION`.

Both are commits, set in `__main__.py`, so every task serves the same corpus. To serve a new build, change both in one PR: the artifacts revision must hold a build that ord-app's pinned ord-schema and RDKit read, and the sources revision must mirror the ord-data commit that build's commit message names. A pair that does not match fails the task at startup.

The revisions in place (ord-artifacts `6662a8b`, "Derive from ord-data 7048706", and the mirror of ord-data `7048706`) fetch 3.2 GiB of artifacts and 1.2 GiB of sources, 93 datasets and 2,428,291 reactions, as measured in ord-app#912 on 2026-10-16. ord-app reads that build from ord-schema 0.9.5 on, so its `main` must include ord-app#912 before this stack deploys.

## Sizing

| Setting | Value | Why |
| --- | --- | --- |
| CPU and memory | 2 vCPU, 16 GiB | Opening the full corpus took about 7 GiB on a laptop (ord-app's README). 16 GiB is the most Fargate pairs with 2 vCPU. |
| `ORD_VIEWER_MEMORY_LIMIT` | 10240 MiB | DuckDB's cap: the task's memory less 6 GiB for Python, RDKit, and nginx, so DuckDB spills to disk before the task runs out. |
| Ephemeral storage | 50 GiB | The image's layers, the 4.4 GiB corpus, and DuckDB's spill share it; Fargate's default is 20 GiB. |
| Health-check grace period | 30 minutes | A cold start took about 10 minutes on a laptop in ord-app#912: 6.6 to fetch the artifacts, 1.7 to fetch the sources, and 1.2 to pair and open. |

A task starts serving only once it has fetched and opened its corpus, so a deploy keeps the old task until the new one passes its health check, `/api/health`. A task whose corpus fails to fetch or open exits, and ECS starts another.

## /api/ask

`/api/ask` calls a model with the key in the stack's `anthropic_api_key` config secret, which the stack stores in Secrets Manager and injects as `ORD_ANTHROPIC_API_KEY`. Without it, the stack makes no secret and `/api/ask` answers 503, while every other route works. To set it, from this directory:

```sh
pulumi config set --secret anthropic_api_key --stack ord/prod
```

## Deploying

```sh
pulumi preview --stack ord/prod
pulumi up      --stack ord/prod
```
