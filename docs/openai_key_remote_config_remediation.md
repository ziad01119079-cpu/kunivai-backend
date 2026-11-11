# Remote Config `OPENAI_API_KEY` remediation

Firebase Remote Config is client-readable. An OpenAI API key stored there must
be treated as exposed even if the Flutter code no longer reads the parameter.
This procedure deliberately records only whether a value exists, never the
value itself.

Read-only verification on 2026-11-12 classified production Remote Config
template version 73 as `PRESENT_WITH_NONEMPTY_VALUE`. The value was not printed
or retained by the review. Rotation and removal are therefore required before
the credential can be considered contained.

## Verify without disclosing the value

1. An authorized Firebase administrator exports the current Remote Config
   template to a temporary file on an encrypted workstation. Do not paste the
   template into a ticket, chat, terminal transcript, screenshot, or report.
2. Run:

   `powershell -File scripts/check_remote_config_secret_presence.ps1 -TemplatePath <temporary-template.json>`

3. Record only one result: `ABSENT`, `PRESENT_WITHOUT_NONEMPTY_VALUE`, or
   `PRESENT_WITH_NONEMPTY_VALUE`. Exit code 2 means a non-empty value exists.
4. Delete the temporary template according to the organization's secure-file
   handling policy. Do not commit it. Remote Config version history should be
   considered to have contained the value even after the current field is
   removed.

The Firebase Console may be used instead by an authorized administrator, but
the value must not be copied or included in evidence. Evidence should show the
parameter name, template version, reviewer, time, and the classification only.

## Rotate if populated or ever published

1. Treat the old key as compromised. Review OpenAI usage, budgets, project
   membership, and audit logs for unexpected activity.
2. Create a replacement key in the correct OpenAI project with the minimum
   required access and explicit usage limits. Never place it in Flutter,
   Firebase Hosting, Remote Config, Docker image layers, or the repository.
3. Put the replacement into the Oracle secret environment used by
   `kunivai-api` (for example a root-owned environment file outside the repo or
   the platform's secret store), then recreate the container from the reviewed
   immutable image.
4. Verify `/health`, `/ready`, one authenticated chat request, and one staged
   knowledge upload/search flow. Check container logs without printing request
   headers or environment values.
5. Revoke the old OpenAI key immediately after the new container is healthy.
   If active abuse is suspected, revoke first and accept the short outage.
6. Remove `OPENAI_API_KEY` from Remote Config, publish a new template version,
   and confirm with the checker that the current template reports `ABSENT`.
7. Search repository history, CI artifacts, Firebase version history, Hosting
   artifacts, support exports, and operational logs for copies. Rotation is
   still required even if all searchable copies are removed.
8. Monitor OpenAI usage and Oracle application errors after revocation.

## Local-review boundary

The checker and this runbook are the only changes in this phase. Creating,
replacing, revoking, or deleting production credentials and publishing Remote
Config require explicit production approval and are not performed locally.
