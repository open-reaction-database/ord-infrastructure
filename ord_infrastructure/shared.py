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
