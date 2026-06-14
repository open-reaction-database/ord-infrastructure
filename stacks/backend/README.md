# backend

Pulumi stack for the backend AWS infrastructure: VPC, RDS Aurora cluster, Redis, and a bastion for local DB access.

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
- Database: `ord`
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

From the VM, fetch the database DSN the same way as the
[bastion section](#connect) — the instance role is allowed to read it.

## Read-only vs read-write credentials

There are two credential sets in Secrets Manager:

- **`rds_ro_dsn` / `rds_ro_password`** — the `readonly` Postgres role, `SELECT`-only
  across the `app`, `ord`, and `editor` databases. **Use these by default** for any
  inspection, by humans and automation alike.
- **`rds_dsn` / `rds_password`** — the master `ord` user (full read-write). Reserved
  for authorized writes (e.g. dataset loads). Don't use these for routine reads.

This stack owns the `readonly` password and the `rds_ro_*` secrets, but the
Postgres `readonly` role itself (and its grants) is managed declaratively by the
[`database` stack](../database/README.md), which reads `rds_ro_password` to set
the role's password. Deploy `backend` first, then `database` (with the bastion
tunnel open).
