# Launch & release playbook

Everything is prepared; these are the steps only the repo owner can do.

## 1. Publish the repository

```bash
cd ~/Projects/personal/whoop-mcp
gh repo create rajdeepmondaldotcom/whoop-mcp --public --source . --push \
  --description "Your WHOOP data in any AI — Claude, ChatGPT, Cursor & every MCP client. Analysis tools, demo mode, one-command setup."
```

Then on GitHub → repo → ⚙️:
- **Topics:** `whoop` `mcp` `model-context-protocol` `claude` `chatgpt` `health` `fitness` `quantified-self` `sleep` `hrv` `python`
- Enable **private vulnerability reporting** (Security tab) — SECURITY.md references it.
- Add a social-preview image (Settings → General) — a screenshot of a Claude conversation using `get_health_overview` converts best.

## 2. PyPI (makes install one command: `uvx whoop-mcp`)

1. Create the `whoop-mcp` project on PyPI → Settings → **Trusted publishing** →
   add GitHub publisher: owner `rajdeepmondaldotcom`, repo `whoop-mcp`,
   workflow `release.yml`, environment `pypi`.
2. Create the `pypi` environment in GitHub repo Settings → Environments.
3. Tag the release — everything else is automatic (tests → build → GitHub
   release → PyPI publish):

```bash
git tag v1.0.0 && git push origin v1.0.0
```

After PyPI is live, update README install lines to `uvx whoop-mcp setup` (a
TODO comment marks the spot).

## 3. MCP Registry (discoverability inside MCP clients)

`server.json` is ready and the README contains the required
`mcp-name: io.github.rajdeepmondaldotcom/whoop-mcp` marker.

```bash
brew install mcp-publisher        # or download from the registry repo
mcp-publisher login github
mcp-publisher publish
```

(Requires the PyPI package to exist first — the registry verifies it.)

## 4. Tell people (the 10k-users part)

The pitch that works: **"Ask Claude how you slept. Demo mode means you can
try it in 30 seconds without a WHOOP account."**

- **r/whoop** (~400k members) — frame as "I built a free, open-source way to
  ask Claude/ChatGPT about your WHOOP data — it finds your strain→recovery
  patterns". Show a screenshot of a real conversation. Mention privacy: data
  never leaves your machine.
- **awesome-mcp-servers** (punkpeye/awesome-mcp-servers) — PR adding it under
  Health/Fitness ("WHOOP — recovery, sleep, strain, workouts + analysis
  tools, demo mode").
- **X/Twitter** — a 30-second screen recording: `whoop-mcp setup` → ask
  Claude "should I train hard today?". Tag @WHOOP and the MCP community.
- **Hacker News** (Show HN) — title: "Show HN: Ask Claude about your WHOOP
  data (open-source MCP server)". The demo mode + correctness story
  (timezone bucketing, polarity-aware trends) is what HN respects.
- **WHOOP Podcast/community Discord, r/QuantifiedSelf, MCP Discord** —
  lower volume, high relevance.

Cadence: Reddit first (richest target audience), HN once a few stars/issues
exist, X continuously with real conversations as content.

## 5. After launch

- Watch issues for WHOOP API drift (they ship changes mid-year).
- When the `mcp` SDK 2.0 stabilizes (~July 2026), migrate FastMCP→MCPServer
  and drop the `<2` pin in one release.
- Pin a "Show me what you asked" discussion — user-contributed prompts are
  free marketing and feed docs/PROMPTS.md.
