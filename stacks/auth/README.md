# auth

Pulumi stack for **Auth0** tenant resources — currently the `ORD App` application
(the SPA users log in through). Managed via the
[`pulumi-auth0`](https://www.pulumi.com/registry/packages/auth0/) provider.

## Credentials

The provider authenticates with a **machine-to-machine** application authorized for
the Auth0 Management API. It needs `read:clients` + `update:clients` (and
`read:resource_servers` / `read:connections` if those are added here later). Set
them as stack config (the secrets are encrypted into `Pulumi.<stack>.yaml`):

```sh
pulumi -C stacks/auth config set        auth0:domain       open-reaction-database.us.auth0.com
pulumi -C stacks/auth config set --secret auth0:clientId     <m2m client id>
pulumi -C stacks/auth config set --secret auth0:clientSecret <m2m client secret>
```

### Inspecting the tenant

To gather the current config (e.g. before importing a resource) the
[Auth0 CLI](https://github.com/auth0/auth0-cli) is easier than minting a
Management API token by hand:

```sh
auth0 login                   # "as a user" (device flow) or "as a machine" (M2M client id/secret)
auth0 apps list               # or: auth0 api get clients
auth0 apps show <client_id>   # full config for one application
```

`auth0 login` supports machine-to-machine auth, but it stores those credentials for
the **CLI's** own use — the Pulumi provider reads its credentials separately, from
stack config (`auth0:clientId`/`auth0:clientSecret` or `auth0:apiToken`) or the
`AUTH0_*` env vars. It can be the same M2M app, but the CLI's session doesn't carry
into Pulumi, so the provider creds (above) must be set directly.

## Why the resource was imported, not created

The `ORD App` application already existed and is **live production auth** — creating
a new one would orphan it and break every existing login. It was brought under
management with `pulumi import` and the code was written to match its exact current
configuration, verified by a no-op preview, before any change was applied. The
resource is `protect=True` so Pulumi will never delete or replace it.

To adopt the existing application in a fresh state (e.g. new backend):

```sh
pulumi -C stacks/auth import auth0:index/client:Client ord_app <client_id>
pulumi -C stacks/auth preview   # must show no changes before you trust it
```

## Callback / logout URLs

`callbacks` and `allowed_logout_urls` list every environment origin allowed to
complete the login flow (`https://app.open-reaction-database.org` for prod,
`https://app-staging.open-reaction-database.org` for staging). The app derives its
`redirect_uri` from the browser origin, so a new environment must be added here
before its logins will work. Leaving a staging URL listed while staging is torn
down is harmless.
