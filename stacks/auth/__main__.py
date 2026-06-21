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

"""Auth0 tenant resources: the ORD App application.

Managed via the pulumi-auth0 provider, which authenticates with a machine-to-machine
app's Management API credentials. Set them as stack config before deploying (see
README.md):

    pulumi -C stacks/auth config set        auth0:domain       open-reaction-database.us.auth0.com
    pulumi -C stacks/auth config set --secret auth0:clientId     <m2m client id>
    pulumi -C stacks/auth config set --secret auth0:clientSecret <m2m client secret>
"""

import pulumi
import pulumi_auth0 as auth0

# Origins allowed to complete the Auth0 login/logout flow. The app derives its
# redirect_uri from the browser origin (globalThis.location.origin), so every
# environment's origin must be listed here. Staging URLs stay listed even when the
# staging stack is torn down — an unused allowed callback is harmless.
PROD = "https://app.open-reaction-database.org"
STAGING = "https://app-staging.open-reaction-database.org"

# The live production login application. Imported from the existing Auth0 tenant;
# protected so Pulumi will never delete or replace it.
ord_app = auth0.Client(
    "ord_app",
    name="ORD App",
    app_type="spa",
    callbacks=[PROD, STAGING],
    allowed_logout_urls=[PROD, STAGING],
    # Public SPA: authorization_code + PKCE with refresh-token rotation. It has no
    # client_secret, so client_credentials doesn't apply (and Auth0 rejects it with
    # auth method "none"); implicit is legacy and unused by @auth0/auth0-react v2.
    grant_types=["authorization_code", "refresh_token"],
    oidc_conformant=True,
    is_first_party=True,
    custom_login_page_on=True,
    initiate_login_uri=f"{PROD}/login",
    jwt_configuration=auth0.ClientJwtConfigurationArgs(
        alg="RS256",
        lifetime_in_seconds=36000,
        secret_encoded=False,
    ),
    refresh_token=auth0.ClientRefreshTokenArgs(
        rotation_type="rotating",
        expiration_type="expiring",
        token_lifetime=2592000,
        idle_token_lifetime=1296000,
        leeway=0,
        infinite_token_lifetime=False,
        infinite_idle_token_lifetime=False,
    ),
    opts=pulumi.ResourceOptions(protect=True),
)

# Machine-to-machine application for Auth0 Management API access — used by this
# stack's own provider and other admin tooling, so the user-facing SPA above never
# needs the client_credentials grant. Imported; protected.
management_api = auth0.Client(
    "management_api",
    name="ord-infrastructure (Pulumi auth stack)",
    description="Management API access for the ord-infrastructure 'auth' Pulumi stack.",
    app_type="non_interactive",
    grant_types=["client_credentials"],
    oidc_conformant=True,
    is_first_party=True,
    custom_login_page_on=True,
    jwt_configuration=auth0.ClientJwtConfigurationArgs(
        alg="RS256", secret_encoded=False
    ),
    opts=pulumi.ResourceOptions(protect=True),
)

# Authorizes the M2M app for the Management API with the scopes the provider needs
# to manage clients and grants. Protected — deleting it locks the provider (and
# this stack) out of Auth0.
management_api_grant = auth0.ClientGrant(
    "management_api_grant",
    client_id=management_api.client_id,
    audience="https://open-reaction-database.us.auth0.com/api/v2/",
    # No delete:clients / delete:client_grants: the managed resources are protect=True,
    # but that only blocks Pulumi-side deletion — withholding the delete scopes means
    # leaked M2M creds can't delete the prod SPA or this grant via the Management API.
    scopes=[
        "read:clients",
        "create:clients",
        "update:clients",
        "read:client_grants",
        "create:client_grants",
        "update:client_grants",
        "read:resource_servers",
        "read:connections",
        "read:client_keys",
    ],
    opts=pulumi.ResourceOptions(protect=True),
)

pulumi.export("ord_app_client_id", ord_app.client_id)
pulumi.export("management_api_client_id", management_api.client_id)
