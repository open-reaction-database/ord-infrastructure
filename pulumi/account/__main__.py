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

"""Account-scoped resources: IAM, AWS Identity Center (SSO), and S3 public-access controls."""

import json

import pulumi_aws as aws

import pulumi

PROTECT = pulumi.ResourceOptions(protect=True)

current = aws.get_caller_identity()
SSO_INSTANCE_ARN = "arn:aws:sso:::instance/ssoins-7223f32c906c0e43"
IDENTITY_STORE_ID = "d-9067e48f13"
ADMIN_PERMISSION_SET_ARN = "arn:aws:sso:::permissionSet/ssoins-7223f32c906c0e43/ps-b3926ef4b0a5a823"
ADMIN_GROUP_ID = "e41874c8-4001-7040-95a9-752f12a811e4"
SKEARNES_SSO_USER_ID = "c49804b8-60c1-704d-2101-d67a4f3f1a04"
ORG_MANAGEMENT_ACCOUNT_ID = "817965877148"

# Block public access at the account level so every bucket in the account inherits the guardrail.
aws.s3.AccountPublicAccessBlock(
    "account_public_access_block",
    account_id=current.account_id,
    block_public_acls=True,
    block_public_policy=True,
    ignore_public_acls=True,
    restrict_public_buckets=True,
    opts=PROTECT,
)


billing_group = aws.iam.Group(
    "billing_group",
    name="BillingFullAccessGroup",
    opts=PROTECT,
)

billing_group_billing_attach = aws.iam.GroupPolicyAttachment(
    "billing_group_billing_attach",
    group="BillingFullAccessGroup",
    policy_arn="arn:aws:iam::aws:policy/job-function/Billing",
    opts=PROTECT,
)

org_account_access_role = aws.iam.Role(
    "org_account_access_role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{ORG_MANAGEMENT_ACCOUNT_ID}:root"},
                    "Action": "sts:AssumeRole",
                },
            ],
        }
    ),
    name="OrganizationAccountAccessRole",
    opts=PROTECT,
)

org_account_access_role_admin = aws.iam.RolePolicyAttachment(
    "org_account_access_role_admin",
    role="OrganizationAccountAccessRole",
    policy_arn="arn:aws:iam::aws:policy/AdministratorAccess",
    opts=PROTECT,
)

admin_account_assignment = aws.ssoadmin.AccountAssignment(
    "admin_account_assignment",
    instance_arn=SSO_INSTANCE_ARN,
    permission_set_arn=ADMIN_PERMISSION_SET_ARN,
    principal_id=ADMIN_GROUP_ID,
    principal_type="GROUP",
    target_id=current.account_id,
    target_type="AWS_ACCOUNT",
    opts=PROTECT,
)

admin_group = aws.identitystore.Group(
    "admin_group",
    display_name="admin",
    identity_store_id=IDENTITY_STORE_ID,
    opts=PROTECT,
)

skearnes_admin_membership = aws.identitystore.GroupMembership(
    "skearnes_admin_membership",
    group_id=ADMIN_GROUP_ID,
    identity_store_id=IDENTITY_STORE_ID,
    member_id=SKEARNES_SSO_USER_ID,
    opts=PROTECT,
)

admin_permission_set = aws.ssoadmin.PermissionSet(
    "admin_permission_set",
    instance_arn=SSO_INSTANCE_ARN,
    name="AdministratorAccess",
    session_duration="PT12H",
    opts=PROTECT,
)

admin_permission_set_policy = aws.ssoadmin.ManagedPolicyAttachment(
    "admin_permission_set_policy",
    instance_arn=SSO_INSTANCE_ARN,
    managed_policy_arn="arn:aws:iam::aws:policy/AdministratorAccess",
    permission_set_arn=ADMIN_PERMISSION_SET_ARN,
    opts=PROTECT,
)

skearnes_sso_user = aws.identitystore.User(
    "skearnes_sso_user",
    display_name="Steven Kearnes",
    emails={
        "primary": True,
        "type": "work",
        "value": "skearnes@gmail.com",
    },
    identity_store_id=IDENTITY_STORE_ID,
    name={
        "family_name": "Kearnes",
        "given_name": "Steven",
    },
    user_name="skearnes",
    opts=PROTECT,
)

bdeadman = aws.iam.User(
    "bdeadman",
    name="bdeadman",
    opts=PROTECT,
)

bdeadman_change_password = aws.iam.UserPolicyAttachment(
    "bdeadman_change_password",
    policy_arn="arn:aws:iam::aws:policy/IAMUserChangePassword",
    user="bdeadman",
    opts=PROTECT,
)

bdeadman_billing_membership = aws.iam.UserGroupMembership(
    "bdeadman_billing_membership",
    groups=["BillingFullAccessGroup"],
    user="bdeadman",
    opts=PROTECT,
)
