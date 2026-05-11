# Copyright 2025 Open Reaction Database Project Authors
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

"""IAM and AWS Identity Center (SSO) resources."""

import pulumi
import pulumi_aws as aws

PROTECT = pulumi.ResourceOptions(protect=True)

billing_full_access_policy = aws.iam.Policy(
    "billing_full_access_policy",
    name='BillingFullAccess',
    policy='{"Statement":[{"Action":"aws-portal:*","Effect":"Allow","Resource":"*","Sid":"VisualEditor0"}],"Version":"2012-10-17"}',
    opts=PROTECT,
)

billing_group = aws.iam.Group(
    "billing_group",
    name='BillingFullAccessGroup',
    opts=PROTECT,
)

billing_group_billing_attach = aws.iam.GroupPolicyAttachment(
    "billing_group_billing_attach",
    group='BillingFullAccessGroup',
    policy_arn='arn:aws:iam::aws:policy/job-function/Billing',
    opts=PROTECT,
)

billing_group_custom_attach = aws.iam.GroupPolicyAttachment(
    "billing_group_custom_attach",
    group='BillingFullAccessGroup',
    policy_arn='arn:aws:iam::482491871729:policy/BillingFullAccess',
    opts=PROTECT,
)

org_account_access_role = aws.iam.Role(
    "org_account_access_role",
    assume_role_policy='{"Statement":[{"Action":"sts:AssumeRole","Effect":"Allow","Principal":{"AWS":"arn:aws:iam::817965877148:root"}}],"Version":"2012-10-17"}',
    name='OrganizationAccountAccessRole',
    opts=PROTECT,
)

org_account_access_role_inline = aws.iam.RolePolicy(
    "org_account_access_role_inline",
    name='AdministratorAccess',
    policy='{"Version":"2012-10-17","Statement":[{"Action":"*","Effect":"Allow","Resource":"*"}]}',
    role='OrganizationAccountAccessRole',
    opts=PROTECT,
)

admin_account_assignment = aws.ssoadmin.AccountAssignment(
    "admin_account_assignment",
    instance_arn='arn:aws:sso:::instance/ssoins-7223f32c906c0e43',
    permission_set_arn='arn:aws:sso:::permissionSet/ssoins-7223f32c906c0e43/ps-b3926ef4b0a5a823',
    principal_id='e41874c8-4001-7040-95a9-752f12a811e4',
    principal_type='GROUP',
    region='us-east-1',
    target_id='482491871729',
    target_type='AWS_ACCOUNT',
    opts=PROTECT,
)

admin_group = aws.identitystore.Group(
    "admin_group",
    display_name='admin',
    identity_store_id='d-9067e48f13',
    region='us-east-1',
    opts=PROTECT,
)

skearnes_admin_membership = aws.identitystore.GroupMembership(
    "skearnes_admin_membership",
    group_id='e41874c8-4001-7040-95a9-752f12a811e4',
    identity_store_id='d-9067e48f13',
    member_id='c49804b8-60c1-704d-2101-d67a4f3f1a04',
    region='us-east-1',
    opts=PROTECT,
)

admin_permission_set = aws.ssoadmin.PermissionSet(
    "admin_permission_set",
    instance_arn='arn:aws:sso:::instance/ssoins-7223f32c906c0e43',
    name='AdministratorAccess',
    region='us-east-1',
    session_duration='PT12H',
    opts=PROTECT,
)

admin_permission_set_policy = aws.ssoadmin.ManagedPolicyAttachment(
    "admin_permission_set_policy",
    instance_arn='arn:aws:sso:::instance/ssoins-7223f32c906c0e43',
    managed_policy_arn='arn:aws:iam::aws:policy/AdministratorAccess',
    permission_set_arn='arn:aws:sso:::permissionSet/ssoins-7223f32c906c0e43/ps-b3926ef4b0a5a823',
    region='us-east-1',
    opts=PROTECT,
)

skearnes_sso_user = aws.identitystore.User(
    "skearnes_sso_user",
    display_name='Steven Kearnes',
    emails={
        "primary": True,
        "type": 'work',
        "value": 'skearnes@gmail.com',
    },
    identity_store_id='d-9067e48f13',
    name={
        "family_name": 'Kearnes',
        "given_name": 'Steven',
    },
    region='us-east-1',
    user_name='skearnes',
    opts=PROTECT,
)

bdeadman = aws.iam.User(
    "bdeadman",
    name='bdeadman',
    opts=PROTECT,
)

bdeadman_change_password = aws.iam.UserPolicyAttachment(
    "bdeadman_change_password",
    policy_arn='arn:aws:iam::aws:policy/IAMUserChangePassword',
    user='bdeadman',
    opts=PROTECT,
)

bdeadman_billing_membership = aws.iam.UserGroupMembership(
    "bdeadman_billing_membership",
    groups=[
        'BillingFullAccessGroup',
    ],
    user='bdeadman',
    opts=PROTECT,
)

