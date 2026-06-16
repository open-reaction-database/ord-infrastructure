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

"""Helpers shared across the Pulumi projects in this repo."""

import json
import os
import subprocess
import sys
from collections.abc import Sequence

import pulumi
import pulumi_aws as aws
import pulumi_awsx as awsx


def assert_sibling_clean(path: str, branch: str = "main") -> None:
    """Fail fast if a sibling repo isn't on `branch` and up to date with origin.

    Used before docker-image builds to keep production deploys honest about
    which commit shipped. Set PULUMI_ALLOW_DIRTY=1 to override when you need
    to deploy a local hotfix.
    """
    if os.environ.get("PULUMI_ALLOW_DIRTY"):
        return
    if subprocess.run(["git", "-C", path, "diff", "--quiet", "HEAD"]).returncode != 0:
        sys.exit(f"ERROR: {path} has uncommitted changes; set PULUMI_ALLOW_DIRTY=1 to override")
    actual_branch = subprocess.run(
        ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if actual_branch != branch:
        sys.exit(f"ERROR: {path} is on '{actual_branch}', expected '{branch}'; set PULUMI_ALLOW_DIRTY=1 to override")
    if subprocess.run(["git", "-C", path, "fetch", "--quiet", "origin", branch]).returncode != 0:
        sys.exit(f"ERROR: failed to fetch {path} origin/{branch}; set PULUMI_ALLOW_DIRTY=1 to override")
    local = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    remote = subprocess.run(
        ["git", "-C", path, "rev-parse", f"origin/{branch}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if local != remote:
        sys.exit(
            f"ERROR: {path} is at {local[:7]} but origin/{branch} is at {remote[:7]}; pull or set PULUMI_ALLOW_DIRTY=1"
        )


def sibling_head(path: str) -> str:
    """Return the sibling repo's HEAD commit, for stamping image provenance.

    Passed to the docker build as the ``GIT_COMMIT`` build arg, which the Dockerfile
    records as the ``org.opencontainers.image.revision`` label — so the deployed image
    says exactly which commit produced it instead of leaving it to be inferred from
    build timestamps. Appends ``-dirty`` when the tree has uncommitted changes; prod is
    gated clean by :func:`assert_sibling_clean`, but staging may build a dirty branch.

    Args:
        path: Path to the sibling repo.

    Returns:
        The 40-character HEAD SHA, suffixed with ``-dirty`` if the working tree is not
        clean, or ``"unknown"`` if ``path`` is not a usable git repository.
    """
    head = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True)
    if head.returncode != 0:
        return "unknown"
    sha = head.stdout.strip()
    # `git status --porcelain` (not `diff --quiet HEAD`) so untracked files count as dirty
    # too — the Dockerfile may COPY a new-but-uncommitted file into the image. If status
    # itself fails we can't confirm clean, so assume dirty rather than mislabel a release.
    status = subprocess.run(["git", "-C", path, "status", "--porcelain"], capture_output=True, text=True)
    dirty = status.returncode != 0 or bool(status.stdout.strip())
    return f"{sha}-dirty" if dirty else sha


def make_ecs_execution_role(
    name: str,
    secret_arns: Sequence[pulumi.Input[str]],
) -> aws.iam.Role:
    """Create an ECS task execution role with read access to specific Secrets Manager secrets.

    Attaches the AWS-managed AmazonECSTaskExecutionRolePolicy (ECR pulls + CloudWatch Logs)
    plus an inline policy granting secretsmanager:GetSecretValue on each ARN in `secret_arns` —
    the permissions ECS needs to resolve `secrets[].value_from` at container start.
    """
    role = aws.iam.Role(
        name,
        assume_role_policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    },
                ],
            }
        ),
    )
    aws.iam.RolePolicyAttachment(
        f"{name}_task_execution",
        role=role.name,
        policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
    )

    def _policy_doc(arns: list[str]) -> str:
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
        f"{name}_secrets",
        role=role.id,
        policy=pulumi.Output.all(*secret_arns).apply(_policy_doc),  # ty: ignore[missing-argument, invalid-argument-type]
    )
    return role


def make_web_service(
    *,
    backend: pulumi.StackReference,
    domain: pulumi.StackReference,
    container_port: int,
    certificate_arn: pulumi.Input[str],
    record_name: pulumi.Input[str],
    sibling_path: str,
    dockerfile: str,
    secret_arns: Sequence[pulumi.Input[str]],
    environment: Sequence[awsx.ecs.TaskDefinitionKeyValuePairArgs] | None = None,
    secrets: Sequence[awsx.ecs.TaskDefinitionSecretArgs] | None = None,
    enforce_clean: bool = True,
    name_prefix: str | None = None,
    cpu: int = 4096,
    memory: int = 8192,
    cluster_name: str | None = None,
) -> awsx.ecs.FargateService:
    """Provision a public-facing ECS Fargate web service behind an ALB.

    Builds the full stack shared by the `app` and `interface` projects: an HTTP→HTTPS
    redirecting ALB, a Route 53 alias to it, an ECR image built from a sibling repo
    (gated by `assert_sibling_clean`), and a Fargate service wired to the backend VPC.

    Resource names are fixed (e.g. "service", "load_balancer"), so call this at most
    once per Pulumi project; the URNs stay stable across the two callers because each
    runs in its own project.

    Args:
        backend: StackReference to `ord/backend/prod` (VPC, subnets).
        domain: StackReference to `ord/domain/prod` (hosted zone).
        container_port: Port the container listens on; also the ALB target/health port.
        certificate_arn: ACM certificate ARN for the HTTPS listener.
        record_name: Fully-resolved DNS name for the Route 53 alias record.
        sibling_path: Path to the sibling repo to build the image from, relative to
            the working directory (the calling stack's project directory).
        dockerfile: Path to the Dockerfile, relative to the working directory (as
            passed to `docker build -f`) — not relative to `sibling_path`.
        secret_arns: Secrets Manager ARNs the execution role may read. Every ARN
            referenced by a `secrets` entry's `value_from` must also be listed here,
            or the task will fail to start with an IAM authorization error.
        environment: Plain environment variables for the container.
        secrets: Secrets injected into the container via the ECS `secrets` directive.
            See `secret_arns` — the two must be kept in sync.
        enforce_clean: If True (default, for prod), require the sibling repo to be on
            a clean `main` before building the image. Set False for staging so the
            current working tree (any branch) can be deployed.
        name_prefix: Explicit physical-name prefix for the ALB and target group
            (alphanumeric + hyphens only — AWS forbids underscores in these names).
            Required for any new environment; leave None for prod so its existing
            auto-generated names are preserved.
        cpu: Fargate task CPU units (default 4096 = 4 vCPU). Must form a valid
            Fargate CPU/memory combination.
        memory: Fargate task memory in MiB (default 8192 = 8 GB).
        cluster_name: Explicit ECS cluster name (e.g. "app", "app-staging",
            "interface") so clusters are distinguishable in the console. None
            auto-generates a "cluster-*" name. Changing it replaces the cluster.

    Returns:
        The created FargateService.

    Raises:
        ValueError: If ``name_prefix`` is too long; AWS caps ALB/target-group names
            at 32 chars, and the longest derived name is ``f"{name_prefix}-dtg"``.
    """
    if name_prefix is not None and len(name_prefix) > 28:
        raise ValueError(f"name_prefix {name_prefix!r} is too long (max 28 chars; derived names append up to '-dtg')")
    target_group = aws.lb.TargetGroup(
        "target_group",
        name=f"{name_prefix}-tg" if name_prefix else None,
        port=container_port,
        protocol="HTTP",
        target_type="ip",
        vpc_id=backend.get_output("vpc_id"),
    )
    load_balancer = awsx.lb.ApplicationLoadBalancer(
        "load_balancer",
        name=name_prefix,
        # awsx always creates a default target group named after this component's
        # logical name ("load_balancer" → an invalid underscore name on a fresh
        # deploy). The listeners forward to `target_group`, so the default is unused
        # — but it still needs a valid name. (prod keeps its existing one.)
        default_target_group=(
            awsx.lb.TargetGroupArgs(
                name=f"{name_prefix}-dtg",
                port=container_port,
                protocol="HTTP",
                target_type="ip",
                vpc_id=backend.get_output("vpc_id"),  # required by AWS when target_type is "ip"
            )
            if name_prefix
            else None
        ),
        listeners=[
            awsx.lb.ListenerArgs(
                default_actions=[
                    aws.lb.ListenerDefaultActionArgs(
                        type="redirect",
                        redirect=aws.lb.ListenerDefaultActionRedirectArgs(
                            port="443", protocol="HTTPS", status_code="HTTP_301"
                        ),
                    )
                ],
                port=80,
                protocol="HTTP",
            ),
            awsx.lb.ListenerArgs(
                certificate_arn=certificate_arn,
                default_actions=[aws.lb.ListenerDefaultActionArgs(type="forward", target_group_arn=target_group.arn)],
                port=443,
                protocol="HTTPS",
            ),
        ],
        subnet_ids=backend.get_output("public_subnet_ids"),
    )

    aws.route53.Record(
        "alias",
        aliases=[
            aws.route53.RecordAliasArgs(
                evaluate_target_health=False,
                name=load_balancer.load_balancer.dns_name,
                zone_id=load_balancer.load_balancer.zone_id,
            )
        ],
        name=record_name,
        type=aws.route53.RecordType.A,
        zone_id=domain.get_output("zone_id"),
    )

    repository = awsx.ecr.Repository(
        "repository",
        awsx.ecr.RepositoryArgs(force_delete=True),
    )

    if enforce_clean:
        assert_sibling_clean(sibling_path)
    image = awsx.ecr.Image(
        "image",
        awsx.ecr.ImageArgs(
            repository_url=repository.url,
            context=sibling_path,
            dockerfile=dockerfile,
            platform="linux/amd64",
            # Stamp the source commit into the image (read back as the
            # org.opencontainers.image.revision label) so a deployed image is traceable
            # to a commit without correlating build timestamps against git history.
            args={"GIT_COMMIT": sibling_head(sibling_path)},
        ),
    )

    security_group = aws.ec2.SecurityGroup(
        "security_group",
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
                from_port=container_port,
                to_port=container_port,
                protocol="tcp",
                cidr_blocks=[backend.get_output("vpc_cidr_block")],
            )
        ],
        vpc_id=backend.get_output("vpc_id"),
    )

    cluster = aws.ecs.Cluster("cluster", name=cluster_name)

    execution_role = make_ecs_execution_role("execution_role", secret_arns)

    return awsx.ecs.FargateService(
        "service",
        awsx.ecs.FargateServiceArgs(
            cluster=cluster.arn,
            load_balancers=[
                aws.ecs.ServiceLoadBalancerArgs(
                    container_name="container", container_port=container_port, target_group_arn=target_group.arn
                )
            ],
            network_configuration=aws.ecs.ServiceNetworkConfigurationArgs(
                subnets=backend.get_output("private_subnet_ids"),
                security_groups=[security_group.id],
            ),
            task_definition_args=awsx.ecs.FargateServiceTaskDefinitionArgs(
                execution_role=awsx.awsx.DefaultRoleWithPolicyArgs(role_arn=execution_role.arn),
                container=awsx.ecs.TaskDefinitionContainerDefinitionArgs(
                    name="container",
                    image=image.image_uri,
                    cpu=cpu,
                    memory=memory,
                    essential=True,
                    port_mappings=[
                        awsx.ecs.TaskDefinitionPortMappingArgs(container_port=container_port, host_port=container_port)
                    ],
                    environment=environment,
                    secrets=secrets,
                ),
            ),
        ),
    )
