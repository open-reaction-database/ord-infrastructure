# account

Pulumi stack for account-scoped resources: IAM, IAM Identity Center (SSO), the cross-account `OrganizationAccountAccessRole`, and account-level S3 controls. Holds the access path you use to manage everything else — accidental deletion would lock you out.

## What's in here

- **SSO admin path**: permission set `AdministratorAccess`, `admin` group in the built-in identity store, account assignment binding the group to the permission set, your SSO user, and your membership in `admin`.
- **`OrganizationAccountAccessRole`** with the managed `AdministratorAccess` policy attached — used by the AWS Organizations management account to administer this member account.
- **Billing access** for `bdeadman`: IAM user, group membership in `BillingFullAccessGroup`, and the `IAMUserChangePassword` attachment so the user can rotate their own password.
- **Account-level S3 Block Public Access** with all four flags `true`, blocking public exposure for every present and future bucket in the account.

## `PROTECT`

Every resource declares `opts=PROTECT` (= `pulumi.ResourceOptions(protect=True)`). Pulumi will refuse to delete a protected resource even if you remove it from this file, so a misplaced `git rm` or accidental commit won't wipe your SSO admin access.

To actually retire a resource:

1. Drop `opts=PROTECT` from the declaration, `pulumi up` to flip the flag, **or** run `pulumi state unprotect <urn> -y` directly.
2. Remove the resource from this file and `pulumi up` to destroy.

## Renaming

Pulumi resource names are part of the URN. Renaming in code alone makes Pulumi see destroy+create. Use `pulumi state rename <old-urn> <new-name>` to rename safely without touching AWS.
