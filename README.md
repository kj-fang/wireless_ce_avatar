# IntelAvatar

Wireless CE log analysis and troubleshooting tool.

## Downloads

| Build | Download |
|---|---|
| Latest nightly | [Download latest nightly](https://github.com/kj-fang/wireless_ce_avatar/releases/tag/nightly-latest) |
| Latest stable release | [Download latest stable release](https://github.com/kj-fang/wireless_ce_avatar/releases/latest) |

The nightly build is produced from `main` after a successful GitHub Actions build. It is published as the `nightly-latest` prerelease and may change with every merge.

Stable releases are produced from `release/X.Y` branches and use tags such as `v1.2.0` and `v1.2.1`.

See [VERSION_CONTROL.md](VERSION_CONTROL.md) for the complete branching, versioning, hotfix, and release procedure.

See [IPS_CASE_NUMBER_FLOW.md](IPS_CASE_NUMBER_FLOW.md) for how a log gets its IPS case number across the four entry points (case box, upload, Send To, straight into the chatbot), and what each branch of the gate is protecting.

## Troubleshooting PF build lookups

WPP/DDD parsing queries Potato Farm (PF) for build details and tries the fallback
site if the first lookup fails. Check the `WPP_DDD_parser` logs for each attempt:

- HTTP failures include the status, reason, requested URL, final response URL,
  and content type. A failed lookup alone does not mean the API is missing.
- Network failures include the request exception type and message.
- Invalid JSON and malformed or missing matching driver builds are reported
  separately from network failures.

For HTTP 401/403, check PF credentials and access permissions. For HTTP 404,
check the API path and whether PF has the requested build. For HTTP 5xx, check
PF service availability. These are troubleshooting leads, not confirmed causes.
Response bodies and API request headers are not logged.
