# viewer

Pulumi project for the ORD viewer, ord-app's public search and browse site: ECS Fargate service, a rule on the backend's shared load balancer, ECR image, and DNS. It serves the apex, `open-reaction-database.org`, from one stack, `ord/prod`, built from ord-app's `deploy/viewer/Dockerfile` on a clean `main`.

## Corpus

The viewer needs no database. Each task fetches its corpus from Hugging Face when it starts, into `/app/corpus` on the task's ephemeral storage:

- the derived artifacts from [ord-artifacts](https://huggingface.co/datasets/open-reaction-database/ord-artifacts) at `ARTIFACTS_REVISION`, and
- their source datasets from the [ord-data mirror](https://huggingface.co/datasets/open-reaction-database/ord-data) at `SOURCES_REVISION`.

Both are commits, set in `__main__.py`, so every task serves the same corpus. To serve a new build, change both in one PR: the artifacts revision must hold a build that ord-app's pinned ord-schema and RDKit read, and the sources revision must mirror the ord-data commit that build's commit message names. A pair that does not match fails the task at startup.

The revisions in place (ord-artifacts `6662a8b`, "Derive from ord-data 7048706", and the mirror of ord-data `7048706`) fetch 3.2 GiB of artifacts and 1.2 GiB of sources, 93 datasets and 2,428,291 reactions, as measured in ord-app#912 on 2026-10-16.

## Sizing

| Setting | Value | Why |
| --- | --- | --- |
| CPU and memory | 2 vCPU, 16 GiB | In a container capped at 2 CPUs and 15 GiB, opening the full corpus peaked at 7.0 GiB, page cache included (2026-10-17, ord-app `7de6638`). 16 GiB is the most Fargate pairs with 2 vCPU. |
| `ORD_VIEWER_MEMORY_LIMIT` | 10240 MiB | DuckDB's cap: the task's memory less 6 GiB for Python, RDKit, and nginx, so DuckDB spills to disk before the task runs out. |
| Ephemeral storage | 50 GiB | The image's layers, the 4.4 GiB corpus, and DuckDB's spill share it; Fargate's default is 20 GiB. |
| Health-check grace period | 30 minutes | That container passed its health check 9.4 minutes after starting, fetching without a token: 5.6 to fetch the artifacts, 1.0 to fetch the sources, and 2.6 to pair and open. The first Fargate task, also without a token, passed it 5.3 minutes after it began running (2026-10-17). |

A task starts serving only once it has fetched and opened its corpus, so a deploy keeps the old task until the new one passes its health check, `/api/health`. A task whose corpus fails to fetch or open exits, and ECS starts another.

## Secrets

Two optional config secrets each go to Secrets Manager and reach the task as a variable:

| Config key | Variable | Without it |
| --- | --- | --- |
| `anthropic_api_key` | `ORD_ANTHROPIC_API_KEY` | `/api/ask` answers 503; every other route works. |
| `hf_token` | `HF_TOKEN` | The startup fetch's thousands of requests to Hugging Face go unauthenticated, under lower rate limits. A read-only token is enough. It buys rate-limit headroom, not speed: on Fargate the artifacts took 1.4 minutes to fetch with a token and 1.3 to 1.7 without (2026-10-17). |

The task names each secret's version, so a new value reaches the service on the next `up`, which replaces the running tasks. To set one, from this directory:

```sh
pulumi config set --secret <config key> --stack ord/prod
```

## Deploying

```sh
pulumi preview --stack ord/prod
pulumi up      --stack ord/prod
```

The **Deploy the viewer** workflow runs the same commands from GitHub Actions; see [Deploying from GitHub Actions](../../README.md#from-github-actions).

`up` waits for the service to reach a steady state, which includes the new task's cold start, for up to the provider's 20-minute default for an ECS service. If a start outlasts that, `up` fails while ECS goes on starting the task; run `up` again once the service is healthy.

### The apex

This stack owns the apex's Route 53 alias record and priority 100 on the backend's shared HTTPS listener. Another stack holding either makes `up` fail: the listener rule's priority is taken, and the record already exists. Retire that stack before the first `up` here, from its own project directory, and expect the apex to be down until the viewer's first task passes its health check:

```sh
pulumi destroy --stack ord/prod
pulumi stack rm ord/prod
```
