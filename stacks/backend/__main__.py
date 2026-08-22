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

"""Shared backend infrastructure: VPC, RDS Aurora, Redis, and an SSM bastion."""

import json
from urllib.parse import quote

import pulumi
import pulumi_aws as aws
import pulumi_awsx as awsx
import pulumi_random as random

vpc = awsx.ec2.Vpc(
    "vpc",
    awsx.ec2.VpcArgs(
        nat_gateways=awsx.ec2.NatGatewayConfigurationArgs(
            strategy=awsx.ec2.NatGatewayStrategy.SINGLE,
        ),
    ),
)

cluster_security_group = aws.ec2.SecurityGroup(
    "cluster_security_group",
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            from_port=5432,
            to_port=5432,
            protocol="tcp",
            cidr_blocks=[vpc.vpc.cidr_block],
        )
    ],
    vpc_id=vpc.vpc_id,
)

cluster_subnet_group = aws.rds.SubnetGroup(
    "cluster_subnet_group", subnet_ids=vpc.private_subnet_ids
)

rds_password = random.RandomPassword(
    "rds_password", length=16, special=True, override_special="!#$%&*()-_=+[]{}<>:?"
)

# Random suffix for the final snapshot name: stable in state (no per-run drift),
# and regenerated if the cluster is ever recreated, so a second teardown can't
# collide with a leftover snapshot from the first.
final_snapshot_suffix = random.RandomId("final_snapshot_suffix", byte_length=4)

# Custom cluster parameters. random_page_cost defaults to 4 (spinning-disk era);
# Aurora storage is SSD-backed, so 1.1 stops the planner over-penalizing index
# scans relative to sequential scans -- important for the RDKit GiST substructure
# and fingerprint indexes. Dynamic parameter, applied without a reboot.
# Single source of truth for the PostgreSQL major version: the cluster pins this
# as its engine_version (major only, so AWS minor auto-upgrades still apply without
# drift) and the parameter group derives its family from it, so the two can't fall
# out of sync. A major-version upgrade is a one-line change here (plus replacing the
# protected parameter group).
POSTGRES_MAJOR = "16"

cluster_parameter_group = aws.rds.ClusterParameterGroup(
    "cluster_parameter_group",
    # Parameter-group names must be lowercase/hyphenated; name_prefix lets the
    # group be replaced without a name collision.
    name_prefix="ord-cluster-",
    family=f"aurora-postgresql{POSTGRES_MAJOR}",
    description="ORD cluster parameters (SSD-appropriate planner costs).",
    parameters=[
        aws.rds.ClusterParameterGroupParameterArgs(
            name="random_page_cost",
            value="1.1",
            apply_method="immediate",
        ),
    ],
    # Consistent with the cluster/instance: surface accidental removal as a Pulumi
    # guardrail rather than an AWS in-use error.
    opts=pulumi.ResourceOptions(protect=True),
)

cluster = aws.rds.Cluster(
    "cluster",
    cluster_identifier="cluster",
    apply_immediately=True,
    database_name="ord",
    db_subnet_group_name=cluster_subnet_group.name,
    db_cluster_parameter_group_name=cluster_parameter_group.name,
    engine=aws.rds.EngineType.AURORA_POSTGRESQL,
    # Major version only, so the parameter group family stays in lockstep while AWS
    # minor auto-upgrades (16.x) still flow. After the first apply this stabilizes
    # (state stores "16"; the running minor surfaces via engine_version_actual), so
    # there's no perpetual diff -- the trap you'd hit by pinning a full "16.11".
    engine_version=POSTGRES_MAJOR,
    engine_mode=aws.rds.EngineMode.PROVISIONED,
    master_username="ord",
    master_password=rds_password.result,
    # Hold the line against accidental teardown of the production database:
    # deletion_protection blocks deletion at the AWS API, and a final snapshot is
    # taken if the cluster is ever deleted anyway.
    deletion_protection=True,
    skip_final_snapshot=False,
    final_snapshot_identifier=pulumi.Output.concat(
        "cluster-final-snapshot-", final_snapshot_suffix.hex
    ),
    storage_encrypted=True,
    # Automated backups: 30 days of continuous point-in-time recovery. The retention
    # period is the TTL — backups older than 30 days auto-expire. Storage is free up
    # to the cluster volume size, so this is ~free for a database this small.
    backup_retention_period=30,
    # Set both windows explicitly so they can't overlap (AWS rejects overlapping
    # backup/maintenance windows; an auto-assigned maintenance window might).
    preferred_backup_window="07:00-08:00",
    preferred_maintenance_window="sun:05:00-sun:06:00",
    copy_tags_to_snapshot=True,
    vpc_security_group_ids=[cluster_security_group.id],
    # Pulumi-side guardrail: refuse to delete even if the resource is removed from code.
    opts=pulumi.ResourceOptions(protect=True),
)

rds_password_secret = aws.secretsmanager.Secret("rds_password")
aws.secretsmanager.SecretVersion(
    "rds_password_secret_version",
    aws.secretsmanager.SecretVersionArgs(
        secret_id=rds_password_secret.id, secret_string=rds_password.result
    ),
)
rds_dsn_secret = aws.secretsmanager.Secret("rds_dsn")
aws.secretsmanager.SecretVersion(
    "rds_dsn_secret_version",
    aws.secretsmanager.SecretVersionArgs(
        secret_id=rds_dsn_secret.id,
        secret_string=pulumi.Output.format(
            "postgresql+psycopg://ord:{0}@{1}:5432/app",
            rds_password.result,
            cluster.endpoint,
        ),
    ),
)

# Read-only credentials for database access (humans and automation alike). The
# `readonly` role and its grants are managed by the `database` stack (see
# stacks/database/README.md); this stack owns the generated password and the
# secrets consumers read. The master (read-write) credentials above are reserved
# for authorized writes.
readonly_password = random.RandomPassword(
    "readonly_password",
    length=16,
    special=True,
    override_special="!#$%&*()-_=+[]{}<>:?",
)
rds_ro_password_secret = aws.secretsmanager.Secret("rds_ro_password")
aws.secretsmanager.SecretVersion(
    "rds_ro_password_secret_version",
    aws.secretsmanager.SecretVersionArgs(
        secret_id=rds_ro_password_secret.id, secret_string=readonly_password.result
    ),
)
rds_ro_dsn_secret = aws.secretsmanager.Secret("rds_ro_dsn")
aws.secretsmanager.SecretVersion(
    "rds_ro_dsn_secret_version",
    aws.secretsmanager.SecretVersionArgs(
        secret_id=rds_ro_dsn_secret.id,
        # Percent-encode the password: it's embedded in a URI, and the random
        # special characters would otherwise corrupt parsing (e.g. `#`, `?`, `@`).
        secret_string=pulumi.Output.format(
            "postgresql+psycopg://readonly:{0}@{1}:5432/app",
            readonly_password.result.apply(lambda pw: quote(pw, safe="")),  # ty: ignore[missing-argument, invalid-argument-type]
            cluster.endpoint,
        ),
    ),
)

cluster_instance = aws.rds.ClusterInstance(
    "cluster_instance",
    identifier="cluster-instance-0",
    cluster_identifier=cluster.id,
    engine=aws.rds.EngineType.AURORA_POSTGRESQL,
    engine_version=cluster.engine_version,
    # Provisioned Graviton instance (8 GB -> ~4.8 GB shared_buffers) so the search
    # working set stays resident -- RAM is the binding constraint. That set is ~2.5 GB
    # of indexes (the RDKit GiST predicate indexes plus the btrees the joins walk),
    # more than db.t4g.medium's ~2 GB cache holds under mixed load. Changing the class
    # is an in-place compute swap (data lives in the shared cluster volume). Size up to
    # db.r7g.large (dedicated CPU) if load grows. See README "Database sizing".
    instance_class="db.t4g.large",
    # Let AWS apply minor (16.x) patches automatically; the cluster pins only the
    # major version, so these don't fight the IaC config.
    auto_minor_version_upgrade=True,
    # Apply instance modifications (e.g. class changes) at once rather than queuing
    # them for the maintenance window, matching the cluster's apply_immediately.
    apply_immediately=True,
    opts=pulumi.ResourceOptions(protect=True),
)

redis_security_group = aws.ec2.SecurityGroup(
    "redis_security_group",
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            from_port=6379,
            to_port=6379,
            protocol="tcp",
            cidr_blocks=[vpc.vpc.cidr_block],
        )
    ],
    vpc_id=vpc.vpc_id,
)
redis = aws.elasticache.ServerlessCache(
    "redis",
    name="redis",
    engine="redis",
    cache_usage_limits={
        "data_storage": {
            "maximum": 10,
            "unit": "GB",
        },
        "ecpu_per_seconds": [
            {
                "maximum": 5000,
            }
        ],
    },
    security_group_ids=[redis_security_group.id],
    subnet_ids=vpc.private_subnet_ids,
)

# Bastion for local DB access via SSM port forwarding (no public IP, no inbound ports).
bastion_role = aws.iam.Role(
    "bastion_role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    ),
)
aws.iam.RolePolicyAttachment(
    "bastion_ssm_policy",
    role=bastion_role.name,
    policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
)
bastion_instance_profile = aws.iam.InstanceProfile(
    "bastion_instance_profile", role=bastion_role.name
)

bastion_security_group = aws.ec2.SecurityGroup(
    "bastion_security_group",
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            from_port=0,
            to_port=0,
            protocol="-1",
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
        )
    ],
    vpc_id=vpc.vpc_id,
)

bastion_ami_id = aws.ssm.get_parameter_output(
    name="/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id",
).value

bastion = aws.ec2.Instance(
    "bastion",
    ami=bastion_ami_id,
    instance_type="t4g.nano",
    iam_instance_profile=bastion_instance_profile.name,
    subnet_id=vpc.private_subnet_ids.apply(lambda ids: ids[0]),  # ty: ignore[missing-argument, invalid-argument-type]
    vpc_security_group_ids=[bastion_security_group.id],
    tags={"Name": "bastion"},
)

# EC2 Instance Connect Endpoint + dev VM for loading datasets into the ORM.
#
# The dev VM has no public IP; SSH reaches it through the Instance Connect
# Endpoint (gated by IAM), e.g.:
#   aws ec2-instance-connect ssh --instance-id "$(pulumi -C stacks/backend stack output dev_vm_instance_id)"
#
# AWS can't launch an instance in the stopped state, and the provider doesn't
# manage power state, so the VM comes up running on first `pulumi up`; stop it
# once and it stays stopped (Pulumi won't restart it). Start/stop on demand:
#   aws ec2 start-instances --instance-ids "$(pulumi -C stacks/backend stack output dev_vm_instance_id)"
#   aws ec2 stop-instances  --instance-ids "$(pulumi -C stacks/backend stack output dev_vm_instance_id)"

# SG on the endpoint itself: it only needs to reach instances in the VPC on 22.
instance_connect_security_group = aws.ec2.SecurityGroup(
    "instance_connect_security_group",
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            from_port=22,
            to_port=22,
            protocol="tcp",
            cidr_blocks=[vpc.vpc.cidr_block],
        )
    ],
    vpc_id=vpc.vpc_id,
)

# SG on the dev VM: SSH only from the Instance Connect Endpoint; open egress.
dev_vm_security_group = aws.ec2.SecurityGroup(
    "dev_vm_security_group",
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            from_port=0,
            to_port=0,
            protocol="-1",
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
        )
    ],
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            from_port=22,
            to_port=22,
            protocol="tcp",
            security_groups=[instance_connect_security_group.id],
        )
    ],
    vpc_id=vpc.vpc_id,
)

instance_connect_endpoint = aws.ec2transitgateway.InstanceConnectEndpoint(
    "instance_connect_endpoint",
    subnet_id=vpc.private_subnet_ids.apply(lambda ids: ids[0]),  # ty: ignore[missing-argument, invalid-argument-type]
    security_group_ids=[instance_connect_security_group.id],
    preserve_client_ip=False,
)

dev_vm_role = aws.iam.Role(
    "dev_vm_role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    ),
)
aws.iam.RolePolicyAttachment(
    "dev_vm_ssm_policy",
    role=dev_vm_role.name,
    policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
)


# Read access to the RDS credentials so the VM can fetch the read-only creds for
# inspection and the master creds for authorized dataset loads.
def _secrets_read_policy(arns: list[str]) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "secretsmanager:GetSecretValue",
                    "Resource": arns,
                },
            ],
        }
    )


aws.iam.RolePolicy(
    "dev_vm_secrets",
    role=dev_vm_role.id,
    policy=pulumi.Output.all(
        rds_password_secret.arn,
        rds_dsn_secret.arn,
        rds_ro_password_secret.arn,
        rds_ro_dsn_secret.arn,
    ).apply(_secrets_read_policy),  # ty: ignore[missing-argument, invalid-argument-type]
)
dev_vm_instance_profile = aws.iam.InstanceProfile(
    "dev_vm_instance_profile", role=dev_vm_role.name
)

dev_vm_ami_id = aws.ssm.get_parameter_output(
    name="/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
).value

dev_vm = aws.ec2.Instance(
    "dev_vm",
    ami=dev_vm_ami_id,
    instance_type="t3.xlarge",
    iam_instance_profile=dev_vm_instance_profile.name,
    subnet_id=vpc.private_subnet_ids.apply(lambda ids: ids[0]),  # ty: ignore[missing-argument, invalid-argument-type]
    vpc_security_group_ids=[dev_vm_security_group.id],
    root_block_device=aws.ec2.InstanceRootBlockDeviceArgs(
        volume_size=50, volume_type="gp3"
    ),
    tags={"Name": "dev-vm"},
    # Both ami and instance_type are ignored so routine deploys leave the VM alone:
    # it holds in-progress dataset work on its root volume and is resized by hand
    # (stop, change type, start) to match the workload — a big dataset load wants more
    # vCPU than day-to-day use. To change either deliberately from code, temporarily
    # drop it from ignore_changes and `pulumi up` — don't taint. An instance_type
    # change is in-place (stop/resize/start, root volume preserved); an ami change
    # forces a replace (new root volume), so only do that when you can lose the volume.
    opts=pulumi.ResourceOptions(ignore_changes=["ami", "instance_type"]),
)

aws.s3.Bucket(
    "ord_bucket",
    bucket="open-reaction-database",
    opts=pulumi.ResourceOptions(protect=True),
)

# The publishable bucket's opposite number. `open-reaction-database` holds artifacts
# meant to be published one day; this holds what never should be -- starting with the
# natural-language question log, which is free text people typed against anonymous
# session identifiers. Keeping them in separate buckets is not about today's
# permissions, since account-level Block Public Access covers both. It is about what
# opening the public one later costs: with two buckets that is a policy change, and
# with one it is an audit of every prefix that has to be right every time.
account_id = aws.get_caller_identity().account_id

internal_bucket = aws.s3.Bucket(
    "ord_internal_bucket",
    bucket="open-reaction-database-internal",
    opts=pulumi.ResourceOptions(protect=True),
)

# Belt and braces over the account-wide block in the `account` stack: this bucket is the
# one where a mistake would matter most, and the setting costs nothing to state twice.
aws.s3.BucketPublicAccessBlock(
    "ord_internal_bucket_public_access",
    bucket=internal_bucket.id,
    block_public_acls=True,
    block_public_policy=True,
    ignore_public_acls=True,
    restrict_public_buckets=True,
)

# Everything the question log holds, which is what access is granted over.
QUESTION_LOG_PREFIX = "nl-log/"
# Where ord_schema.search.nl_log writes, one object per question, and where
# nl_log.compact(redact=True) puts the months it folds them into. The two are separate
# prefixes rather than one nested inside the other so that no object matches both rules
# below: S3 resolves overlapping lifecycle rules by its own precedence, and a design
# that has to be right about that precedence is a design waiting to delete an archive.
QUESTION_LOG_RAW_PREFIX = "nl-log/raw/"
QUESTION_LOG_ARCHIVE_PREFIX = "nl-log/parquet/"
# Where the trail below delivers. Disjoint from the log's prefixes, so a trail watching
# the log does not record its own writes.
AUDIT_PREFIX = "cloudtrail/"

# Two tiers, because the two halves of a record age differently.
#
# The raw objects hold what people typed. That is the half worth retiring early: a
# question is free text, and on this corpus it carries research intent -- what a chemist
# is working on -- more often than it carries anything personal.
#
# The compacted months hold no free text and are what the analysis runs on. Thirteen
# months is a year plus a month of overlap, so this August compares against last August.
# That window is sample size rather than sentiment: the log grows at the rate people ask
# questions, which is slow, and retention is the only dial that buys more of them.
#
# The raw tier deliberately outlives the monthly compaction by a wide margin. Compaction
# is what carries a month into the long tier, so expiring the raw objects at ninety days
# would make one missed run a silent, permanent loss; at two hundred, three runs have to
# fail in a row before anything goes missing.
QUESTION_LOG_RAW_RETENTION_DAYS = 200
QUESTION_LOG_RETENTION_DAYS = 395
# Longer than either, so "who read this" survives the thing that was read.
AUDIT_RETENTION_DAYS = 730

aws.s3.BucketLifecycleConfigurationV2(
    "ord_internal_bucket_lifecycle",
    bucket=internal_bucket.id,
    rules=[
        aws.s3.BucketLifecycleConfigurationV2RuleArgs(
            id="expire-question-log-raw",
            status="Enabled",
            filter=aws.s3.BucketLifecycleConfigurationV2RuleFilterArgs(
                prefix=QUESTION_LOG_RAW_PREFIX
            ),
            expiration=aws.s3.BucketLifecycleConfigurationV2RuleExpirationArgs(
                days=QUESTION_LOG_RAW_RETENTION_DAYS
            ),
        ),
        aws.s3.BucketLifecycleConfigurationV2RuleArgs(
            id="expire-question-log-archive",
            status="Enabled",
            filter=aws.s3.BucketLifecycleConfigurationV2RuleFilterArgs(
                prefix=QUESTION_LOG_ARCHIVE_PREFIX
            ),
            expiration=aws.s3.BucketLifecycleConfigurationV2RuleExpirationArgs(
                days=QUESTION_LOG_RETENTION_DAYS
            ),
        ),
        # The access record outlives the records it describes, so a read can still be
        # attributed after what was read has expired.
        aws.s3.BucketLifecycleConfigurationV2RuleArgs(
            id="expire-audit-log",
            status="Enabled",
            filter=aws.s3.BucketLifecycleConfigurationV2RuleFilterArgs(
                prefix=AUDIT_PREFIX
            ),
            expiration=aws.s3.BucketLifecycleConfigurationV2RuleExpirationArgs(
                days=AUDIT_RETENTION_DAYS
            ),
        ),
        # Ordinary hygiene: a failed upload otherwise leaves parts that are billed and
        # invisible. Applies to the whole bucket, which is why it carries no filter.
        aws.s3.BucketLifecycleConfigurationV2RuleArgs(
            id="abort-incomplete-uploads",
            status="Enabled",
            filter=aws.s3.BucketLifecycleConfigurationV2RuleFilterArgs(prefix=""),
            abort_incomplete_multipart_upload=aws.s3.BucketLifecycleConfigurationV2RuleAbortIncompleteMultipartUploadArgs(
                days_after_initiation=7
            ),
        ),
    ],
)


# Nothing is granted access to the question log yet, deliberately. The records are what
# people typed, so a standing grant wants a reason, and the two candidates do not have
# one yet: nothing serves ord_schema.search.nl, and these stacks build ECS execution
# roles rather than task roles, so there is no identity a running container assumes to
# write as. An eval run reads and writes its own file. Whoever needs this next gets a
# grant scoped to one prefix, with reading separated from listing.


def _cloudtrail_bucket_policy(arguments: list[str]) -> str:
    """Returns the bucket policy letting CloudTrail deliver into its own prefix.

    Args:
        arguments: The bucket ARN and the account ID, in that order.

    Returns:
        The policy document. Delivery is confined to the trail's prefix, and the ACL
        condition is the one CloudTrail sets on every object it writes.
    """
    bucket_arn, account_id = arguments
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "cloudtrail.amazonaws.com"},
                    "Action": "s3:GetBucketAcl",
                    "Resource": bucket_arn,
                },
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "cloudtrail.amazonaws.com"},
                    "Action": "s3:PutObject",
                    "Resource": (f"{bucket_arn}/{AUDIT_PREFIX}AWSLogs/{account_id}/*"),
                    "Condition": {
                        "StringEquals": {"s3:x-amz-acl": "bucket-owner-full-control"}
                    },
                },
            ],
        }
    )


_trail_delivery = pulumi.Output.all(internal_bucket.arn, account_id)
internal_bucket_policy = aws.s3.BucketPolicy(
    "ord_internal_bucket_policy",
    bucket=internal_bucket.id,
    policy=_trail_delivery.apply(_cloudtrail_bucket_policy),  # ty: ignore[missing-argument, invalid-argument-type]
)


def _question_log_selectors(bucket_arn: str) -> list:
    """Returns the data-event selectors naming reads of the question log.

    Args:
        bucket_arn: ARN of the bucket holding the log.

    Returns:
        One advanced selector. PutObject is excluded: a write happens once per question
        and says only what the service already knows, while a read is somebody looking
        at what people typed, which is the thing worth being able to attribute.
    """
    return [
        aws.cloudtrail.TrailAdvancedEventSelectorArgs(
            name="question log reads",
            field_selectors=[
                aws.cloudtrail.TrailAdvancedEventSelectorFieldSelectorArgs(
                    field="eventCategory", equals=["Data"]
                ),
                aws.cloudtrail.TrailAdvancedEventSelectorFieldSelectorArgs(
                    field="resources.type", equals=["AWS::S3::Object"]
                ),
                aws.cloudtrail.TrailAdvancedEventSelectorFieldSelectorArgs(
                    field="resources.ARN",
                    starts_withs=[f"{bucket_arn}/{QUESTION_LOG_PREFIX}"],
                ),
                aws.cloudtrail.TrailAdvancedEventSelectorFieldSelectorArgs(
                    field="eventName", not_equals=["PutObject"]
                ),
            ],
        )
    ]


# Object-level access to the log is recorded, so reading it is attributable rather than
# merely permitted. The trail delivers into this same bucket under a prefix disjoint
# from the log's, which is what keeps it from recording its own deliveries.
question_log_trail = aws.cloudtrail.Trail(
    "question_log_trail",
    s3_bucket_name=internal_bucket.id,
    s3_key_prefix=AUDIT_PREFIX.rstrip("/"),
    include_global_service_events=False,
    is_multi_region_trail=False,
    enable_log_file_validation=True,
    advanced_event_selectors=internal_bucket.arn.apply(_question_log_selectors),  # ty: ignore[missing-argument, invalid-argument-type]
    opts=pulumi.ResourceOptions(depends_on=[internal_bucket_policy]),
)

pulumi.export("internal_bucket", internal_bucket.bucket)
pulumi.export("question_log_prefix", QUESTION_LOG_RAW_PREFIX)
pulumi.export("question_log_archive_prefix", QUESTION_LOG_ARCHIVE_PREFIX)
pulumi.export("question_log_trail", question_log_trail.name)

pulumi.export("vpc_id", vpc.vpc_id)
pulumi.export("vpc_cidr_block", vpc.vpc.cidr_block)
pulumi.export("public_subnet_ids", vpc.public_subnet_ids)
pulumi.export("private_subnet_ids", vpc.private_subnet_ids)
pulumi.export("rds_endpoint", cluster.endpoint)
pulumi.export("rds_password_secret_arn", rds_password_secret.arn)
pulumi.export("rds_dsn_secret_arn", rds_dsn_secret.arn)
pulumi.export("rds_ro_password_secret_arn", rds_ro_password_secret.arn)
pulumi.export("rds_ro_dsn_secret_arn", rds_ro_dsn_secret.arn)
redis_address = redis.endpoints.apply(lambda endpoints: endpoints[0]["address"])  # ty: ignore[missing-argument, invalid-argument-type]
pulumi.export("redis_endpoint", redis_address)
pulumi.export("bastion_instance_id", bastion.id)
pulumi.export("dev_vm_instance_id", dev_vm.id)
pulumi.export("instance_connect_endpoint_id", instance_connect_endpoint.id)
