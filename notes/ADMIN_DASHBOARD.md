# Administration in Settings

The first card on `/settings` is available only to authenticated administrators.
Every `/api/admin` endpoint checks the same database admin flag independently;
mutations also require a same-origin request. Cloudflare identities cannot be
admins. Use the existing SSH tunnel and nginx sign-in for administration.

## Collaborator onboarding

1. Save the collaborator's stable username and Cloudflare email. A new account
   starts excluded from `everyone`, with access only through explicit grants.
   Updating an existing mapping preserves its scope and grants.
2. Create a custom group, add the collaborator, and grant observations to it,
   or grant directly to `user:<username>`. All populated selector fields must
   match; separate grants combine. Dates are inclusive YYMMDD obsdates.
3. Review the displayed memberships and grants. For an observation-level check,
   use `uv run muscat-db access explain <username>`.
4. Add the email to the Cloudflare Access policy separately. The GUI does not
   call Cloudflare or store a Cloudflare API token.

The access-scope control can include the `everyone` group's grants again. This
is broader than explicit grants only; it does not grant every observation.
Revoking one grant may leave access through another group. Revoked grants retain
history and the authenticated administrator's username. Administrative writes
also emit an application log entry with the actor and operation.

Clear an email to block Cloudflare sign-in without deleting the account, grants,
settings, or jobs. Remove the email from the Cloudflare policy when offboarding.
An existing nginx password remains valid and must be removed through the CLI
when all sign-in routes need to be revoked. Admin promotion and nginx password
management remain CLI-only.

## Privileges and operation

These controls write to the application's SQLite database using existing access
functions. They do not invoke sudo, edit nginx configuration or password files,
or restart services. The web process needs its existing database write access.
The dashboard links to Jobs for operational inspection. All account and grant
strings are rendered as text, never injected as HTML. Account-management drafts
are deliberately not persisted in browser localStorage: saved state is reloaded
from the database on each visit, avoiding stale administrative actions.

Deploy and restart the application to see the template and JavaScript changes;
Python `--reload` does not watch Jinja templates. No new service, schema, secret,
or elevated privilege is required.
