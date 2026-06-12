# Security policy

whoop-mcp handles personal health data and OAuth credentials, so security
reports get priority over everything else.

## Reporting

Please **do not** open a public issue for vulnerabilities. Email
**rajdeep@rajdeepmondal.com** with details, or use GitHub's private
vulnerability reporting on this repository. You'll get a response within 72
hours.

## Scope and design notes

- Tokens and app credentials are stored locally (`~/.whoop-mcp`, mode 0600).
  Nothing is transmitted anywhere except `api.prod.whoop.com`.
- The server is read-only against WHOOP; the only writes are local files the
  user requests (`export_data`) and the OAuth flow the user triggers.
- The stdio transport exposes nothing over the network. The HTTP transport
  binds 127.0.0.1 by default; exposing it publicly (for ChatGPT) is the
  user's explicit choice and the README warns about it.
- No telemetry, no analytics, no third-party services.

## Supported versions

The latest release line receives security fixes.
