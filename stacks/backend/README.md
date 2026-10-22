# backend

Pulumi stack for the backend AWS infrastructure: VPC, RDS Aurora cluster, a bastion for local DB access, and a bucket for logs.

## Networking

- **Egress.** The private subnets reach the internet through one managed NAT gateway.
  ECR image layers come from S3 through a gateway endpoint, which is free, rather than
  the NAT gateway, which charges per gigabyte.
- **Ingress.** One application load balancer serves every site. Its HTTPS listener
  holds the apex and wildcard certificates and returns 404 for unclaimed hosts; each
  service stack adds a target group and a host-header rule (`https_listener_arn`).

## Database sizing

The cluster runs one **provisioned** `db.t4g.medium` instance: 4 GB of RAM, of which
Aurora gives about 2 GB to `shared_buffers`. The class is chosen for RAM first: a query
is fast when the pages it reads are already in `shared_buffers`, and slow when it
re-reads them from the cluster volume.

The cluster serves two databases, measured on 2026-10-21 as the `readonly` role:

| database | size | used by |
| --- | --- | --- |
| `app` | 608 MB | ord-app's editor, both the `prod` and `staging` stacks |
| `editor` | 282 MB | nothing that reads its tables: `pg_statio_user_tables` showed no reads since the instance restarted on 2026-10-09 |

The working set is `app`'s `reaction` table, 598 MB of the database:

- **Heap and indexes, 222 MB.** The editor's queries read these: a 151 MB heap and
  71 MB of btree indexes, the largest `uq_pb_reaction_id_dataset_id` (33 MB).
- **TOAST, 377 MB.** The serialized reaction protos, read when a reaction is opened or
  exported. They are touched far less often than the heap: about 76,000 block
  accesses (cache hits plus storage reads) against about 925,000 for the heap and
  indexes.
- The other seven tables (users, groups, datasets, templates) total under 1 MB.

Both databases together (890 MB) take under half of `db.t4g.medium`'s cache. From
2026-10-18, the day after ord-interface was torn down, to 2026-10-21, the instance
(then a `db.t4g.large`) ran at 7 % CPU on average and 10 % at peak, with 4–8
connections, read IOPS near zero, and a daily buffer cache hit ratio above 99.99 %.

Sizing guidance:

- Aurora sets `shared_buffers` to about 60 % of the instance's RAM. Pick a class whose
  `shared_buffers` holds the working set with headroom for `work_mem` and connections.
- `db.t4g.medium` is the smallest class Aurora PostgreSQL offers. It has the same 2
  vCPU as `db.t4g.large`, with a CPU baseline of 20 % per vCPU against the large's
  30 %. Aurora runs T classes in Unlimited mode, so a 24-hour average above the
  baseline is billed (`CPUSurplusCreditsCharged`) rather than throttled.
- Size up to `db.t4g.large` (about 4.7 GB of `shared_buffers`) when the daily buffer
  cache hit ratio drops below 99 % or read IOPS climb under steady load, or when CPU
  averages above the medium's baseline. Size up to `db.r7g.large` for dedicated
  (non-burstable) CPU.
- A structure-search database needs more. ord-interface's `ord_20260702` kept about
  2.5 GB of RDKit GiST and btree indexes hot, and the cluster ran at `db.t4g.large` to
  hold them.
- Serverless v2 would bill for an always-on cluster: the editor holds its connection
  pool open, so the cluster never reaches zero connections to pause on.
- A class change is an in-place compute swap; the data lives in the shared cluster
  volume. The new instance starts with a cold cache and re-reads pages from storage as
  queries touch them. The buffer cache survives ordinary database restarts.
- Reserved instances apply only to provisioned classes (not Serverless v2 ACUs) and
  cut ~25 % off at 1 year; buy one once the class is settled.

## Connecting to RDS from a local client (DataGrip, psql, etc.)

The RDS cluster has no public endpoint. A `t4g.nano` bastion in a private subnet forwards traffic to it via AWS Systems Manager — no SSH, no public IP, no inbound ports. Access is gated by IAM.

### One-time setup

Install the SSM Session Manager plugin:

```sh
brew install --cask session-manager-plugin
```

Make sure your AWS CLI credentials are configured for the account this stack is deployed to.

### Start the tunnel

```sh
BASTION=$(pulumi -C stacks/backend stack output bastion_instance_id)
RDS=$(pulumi -C stacks/backend stack output rds_endpoint)
aws ssm start-session \
  --target "$BASTION" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$RDS\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"15432\"]}"
```

Leave that running. Local port `15432` is now forwarded to the RDS endpoint on `5432`.

### Connect

In DataGrip (or any PostgreSQL client):

- Host: `localhost`
- Port: `15432`
- Database: `app`, or any database listed in the [`database` stack](../database/README.md#what-it-manages)
- User: `ord`
- Password: pulled from the `rds_password` secret in AWS Secrets Manager. To fetch it:

  ```sh
  aws secretsmanager get-secret-value \
    --secret-id "$(pulumi -C stacks/backend stack output rds_password_secret_arn)" \
    --query SecretString --output text
  ```

### Cost note

The bastion runs 24/7 at roughly $3/mo. To pause it when you're not using it:

```sh
aws ec2 stop-instances --instance-ids "$BASTION"
aws ec2 start-instances --instance-ids "$BASTION"
```

## Dev VM (loading datasets into the ORM)

A `t3.xlarge` Ubuntu 24.04 instance (`dev-vm`) for loading datasets into the
database. It has a 50 GB gp3 root volume (grow it later if needed — no replace),
no public IP, and an instance role that can read the RDS credential secrets. SSH reaches it through an EC2 Instance
Connect Endpoint (IAM-gated — no SSH keys or open ports).

**It is meant to stay stopped.** AWS can't launch an instance pre-stopped, so it
comes up running on the first `pulumi up`; stop it once and Pulumi leaves it
alone (the provider doesn't manage power state). Turn it on only when you need
it.

```sh
DEV_VM=$(pulumi -C stacks/backend stack output dev_vm_instance_id)

aws ec2 start-instances --instance-ids "$DEV_VM"   # before a session
aws ec2 stop-instances  --instance-ids "$DEV_VM"   # when done

# Connect (requires the EC2 Instance Connect CLI / SSM plugin):
aws ec2-instance-connect ssh --instance-id "$DEV_VM"
```

The VM is inside the VPC, so it connects to the cluster directly, with no tunnel:

- Host: the `rds_endpoint` stack output
- Port: `5432`
- User and password: `readonly` with `rds_ro_password`, or `ord` with
  `rds_password` for authorized writes (see below)

Look up the endpoint and the secret ARN locally; the VM's instance role can read
both secrets. The VM boots from a stock Ubuntu image, so install the AWS CLI and
`psql` on it once:

```sh
# On the VM, once:
sudo snap install aws-cli --classic
sudo apt-get update && sudo apt-get install -y postgresql-client

# Locally:
pulumi -C stacks/backend stack output rds_endpoint
pulumi -C stacks/backend stack output rds_ro_password_secret_arn

# On the VM, with the values from above:
PGPASSWORD=$(aws secretsmanager get-secret-value --secret-id "<secret ARN>" \
  --query SecretString --output text) \
  psql "host=<rds_endpoint> port=5432 user=readonly dbname=app sslmode=require"
```

## Read-only vs read-write credentials

There are two passwords in Secrets Manager, one per Postgres role:

- **`rds_ro_password`** — the `readonly` role. **Use it by default** for any
  inspection, by humans and automation alike. The role has `SELECT` on every
  database the `database` stack manages.
- **`rds_password`** — the master `ord` user (full read-write). Reserved for
  authorized writes (e.g. dataset loads). Don't use it for routine reads.

This stack owns the `readonly` password and its secret, but the
Postgres `readonly` role itself (and its grants) is managed declaratively by the
[`database` stack](../database/README.md), which reads `rds_ro_password` to set
the role's password. Deploy `backend` first, then `database` (with the bastion
tunnel open).

## The logs bucket

`open-reaction-database-logs` holds the natural-language question log and the
CloudTrail record of access to it. It sets its own public-access block beside the
account-wide one in the [`account` stack](../account/README.md).

| prefix | holds | expires after |
| --- | --- | --- |
| `nl-log/raw/` | one JSON object per question, from `ord_schema.search.nl_log.S3Sink` (its default prefix) | 365 days |
| `nl-log/redacted/` | months compacted by `nl_log.compact(redact=True)`, with the free-text fields emptied | 1825 days |
| `cloudtrail/nl-log/` | the question log trail's log and digest files | 2190 days |

Each rule also expires noncurrent versions a day after they become noncurrent: the
bucket is unversioned, but on a versioned bucket expiring an object only adds a delete
marker. A fourth rule aborts incomplete multipart uploads after 7 days, across the
bucket.

- **Prefixes.** No expiration prefix contains another, so no object matches two
  expiration rules. Where rules overlap, S3 applies the shorter expiration, which
  would expire the redacted months on the raw clock.
- **Clocks.** S3 counts an object's age from its creation. A raw object's clock starts
  when the question is asked; a redacted month's starts when it is compacted, and
  restarts if the month is rewritten. Compaction must run before a month's first raw
  objects expire.
- **Access.** This stack grants no role access to the bucket. The account stack's
  AdministratorAccess identities (the SSO permission set and
  `OrganizationAccountAccessRole`) can read it through their own policies, and the
  trail records those reads. ord-app's viewer serves
  `ord_schema.search.nl` at `/api/ask` but records nothing unless
  `ORD_VIEWER_ASK_LOG=stdout`, which the viewer stack does not set, and
  `make_web_service` gives a task an execution role but no task role. A writer needs a
  task role allowed `s3:PutObject` under `nl-log/raw/`.
- **Trail.** `question-log-access` records every S3 object data event under `nl-log/`
  except `PutObject`, which the writer makes once per question. It delivers under
  `cloudtrail/nl-log/`, outside the log's prefix, so it does not record its own
  deliveries, and the bucket policy admits deliveries from this trail only. A trail
  for another service gets its own folder under `cloudtrail/`: CloudTrail's log file
  names do not name the trail, so trails sharing a folder would mix their files under
  one expiration rule.
