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

cluster_subnet_group = aws.rds.SubnetGroup("cluster_subnet_group", subnet_ids=vpc.private_subnet_ids)

rds_password = random.RandomPassword("rds_password", length=16, special=True, override_special="!#$%&*()-_=+[]{}<>:?")

# Random suffix for the final snapshot name: stable in state (no per-run drift),
# and regenerated if the cluster is ever recreated, so a second teardown can't
# collide with a leftover snapshot from the first.
final_snapshot_suffix = random.RandomId("final_snapshot_suffix", byte_length=4)

cluster = aws.rds.Cluster(
    "cluster",
    cluster_identifier="cluster",
    apply_immediately=True,
    database_name="ord",
    db_subnet_group_name=cluster_subnet_group.name,
    engine=aws.rds.EngineType.AURORA_POSTGRESQL,
    engine_mode=aws.rds.EngineMode.PROVISIONED,
    master_username="ord",
    master_password=rds_password.result,
    # Hold the line against accidental teardown of the production database:
    # deletion_protection blocks deletion at the AWS API, and a final snapshot is
    # taken if the cluster is ever deleted anyway.
    deletion_protection=True,
    skip_final_snapshot=False,
    final_snapshot_identifier=pulumi.Output.concat("cluster-final-snapshot-", final_snapshot_suffix.hex),
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
    serverlessv2_scaling_configuration=aws.rds.ClusterServerlessv2ScalingConfigurationArgs(
        min_capacity=0,
        max_capacity=1,
        seconds_until_auto_pause=3600,
    ),
    vpc_security_group_ids=[cluster_security_group.id],
    # Pulumi-side guardrail: refuse to delete even if the resource is removed from code.
    opts=pulumi.ResourceOptions(protect=True),
)

rds_password_secret = aws.secretsmanager.Secret("rds_password")
aws.secretsmanager.SecretVersion(
    "rds_password_secret_version",
    aws.secretsmanager.SecretVersionArgs(secret_id=rds_password_secret.id, secret_string=rds_password.result),
)
rds_dsn_secret = aws.secretsmanager.Secret("rds_dsn")
aws.secretsmanager.SecretVersion(
    "rds_dsn_secret_version",
    aws.secretsmanager.SecretVersionArgs(
        secret_id=rds_dsn_secret.id,
        secret_string=pulumi.Output.format(
            "postgresql+psycopg://ord:{0}@{1}:5432/app", rds_password.result, cluster.endpoint
        ),
    ),
)

# Read-only credentials for database access (humans and automation alike). The
# `readonly` role and its grants are managed by the `database` stack (see
# stacks/database/README.md); this stack owns the generated password and the
# secrets consumers read. The master (read-write) credentials above are reserved
# for authorized writes.
readonly_password = random.RandomPassword(
    "readonly_password", length=16, special=True, override_special="!#$%&*()-_=+[]{}<>:?"
)
rds_ro_password_secret = aws.secretsmanager.Secret("rds_ro_password")
aws.secretsmanager.SecretVersion(
    "rds_ro_password_secret_version",
    aws.secretsmanager.SecretVersionArgs(secret_id=rds_ro_password_secret.id, secret_string=readonly_password.result),
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
    instance_class="db.serverless",
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
bastion_instance_profile = aws.iam.InstanceProfile("bastion_instance_profile", role=bastion_role.name)

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
        rds_password_secret.arn, rds_dsn_secret.arn, rds_ro_password_secret.arn, rds_ro_dsn_secret.arn
    ).apply(_secrets_read_policy),  # ty: ignore[missing-argument, invalid-argument-type]
)
dev_vm_instance_profile = aws.iam.InstanceProfile("dev_vm_instance_profile", role=dev_vm_role.name)

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
    root_block_device=aws.ec2.InstanceRootBlockDeviceArgs(volume_size=50, volume_type="gp3"),
    tags={"Name": "dev-vm"},
    # The VM holds in-progress dataset work on its root volume; don't let a newer
    # base AMI trigger a replace. Bump deliberately by tainting when you want one.
    opts=pulumi.ResourceOptions(ignore_changes=["ami"]),
)

aws.s3.Bucket(
    "ord_bucket",
    bucket="open-reaction-database",
    opts=pulumi.ResourceOptions(protect=True),
)

pulumi.export("vpc_id", vpc.vpc_id)
pulumi.export("vpc_cidr_block", vpc.vpc.cidr_block)
pulumi.export("public_subnet_ids", vpc.public_subnet_ids)
pulumi.export("private_subnet_ids", vpc.private_subnet_ids)
pulumi.export("rds_endpoint", cluster.endpoint)
pulumi.export("rds_password_secret_arn", rds_password_secret.arn)
pulumi.export("rds_dsn_secret_arn", rds_dsn_secret.arn)
pulumi.export("rds_ro_password_secret_arn", rds_ro_password_secret.arn)
pulumi.export("rds_ro_dsn_secret_arn", rds_ro_dsn_secret.arn)
pulumi.export("redis_endpoint", redis.endpoints.apply(lambda endpoints: endpoints[0]["address"]))  # ty: ignore[missing-argument, invalid-argument-type]
pulumi.export("bastion_instance_id", bastion.id)
pulumi.export("dev_vm_instance_id", dev_vm.id)
pulumi.export("instance_connect_endpoint_id", instance_connect_endpoint.id)
