# Privacy policy

whoop-mcp is open source software you run on your own computer. This page
describes how it handles your data. The short version: your data stays with
you.

## What the software does with your data

- It reads your WHOOP data (recovery, sleep, strain, workouts, profile,
  body measurements) from WHOOP's official API, only after you authorize it
  through WHOOP's own consent screen.
- It processes that data in memory on your machine to answer questions you
  ask through your AI client.
- It stores your OAuth tokens locally on your machine, in a file only your
  user account can read.
- If you ask it to export your data, it writes files to your own disk and
  nowhere else.

## What it never does

- It never sends your data to the author of this software or to any third
  party. The only network connection is to api.prod.whoop.com.
- It never writes or changes anything in your WHOOP account. Access is
  read-only.
- It collects no analytics and no telemetry. There are no accounts, no
  servers, and no databases run by this project.

## What you share with your AI client

When you connect this software to an AI client such as Claude, the answers
computed from your data are sent to that AI as part of your conversation.
That exchange is governed by your AI provider's privacy policy, not this
one. Connect only AI clients you trust with health data.

## Revoking access

Run `whoop-mcp logout --revoke` to revoke the app's access with WHOOP and
delete the local tokens. You can also revoke access any time from your
WHOOP account settings.

## Contact

Questions: rajdeep@rajdeepmondal.com or open an issue at
https://github.com/rajdeepmondaldotcom/whoop-mcp-server/issues.

Last updated: June 12, 2026.
