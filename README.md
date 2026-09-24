<p align="center">
  <img src="docs/logo.svg" width="200" alt="Securo logo" />
</p>
<h1 align="center">Securo</h1>
<p align="center">
  <a href="https://github.com/securo-finance/securo/actions/workflows/ci.yml"><img src="https://github.com/securo-finance/securo/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <img src="https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/tassionoronha/ae627b744aaa2ba89d850ea541c311be/raw/coverage.json" alt="Coverage" />
  <a href="https://github.com/securo-finance/securo/pkgs/container/securo-frontend"><img src="https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/tassionoronha/ae627b744aaa2ba89d850ea541c311be/raw/downloads.json" alt="Downloads" /></a>
  <br />
  <a href="https://artifacthub.io/packages/search?repo=securo"><img src="https://img.shields.io/endpoint?url=https://artifacthub.io/badge/repository/securo" alt="Artifact Hub" /></a>
  <a href="https://www.gnu.org/licenses/agpl-3.0"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue.svg" alt="License: AGPL-3.0" /></a>
  <a href="https://discord.gg/rUqTKtQ9S4"><img src="https://img.shields.io/badge/Discord-Join%20the%20community-5865F2?logo=discord&logoColor=white" alt="Join our Discord" /></a>
  <br />
  <a href="https://usesecuro.com/">Website</a> · <a href="https://demo.usesecuro.com/">Demo</a> · <a href="https://www.usesecuro.com/roadmap">Roadmap</a> · <a href="https://docs.usesecuro.com/">Docs</a> · <a href="https://discord.gg/rUqTKtQ9S4">Discord</a> · <a href="https://cal.com/tassio/15min">Talk to the maintainer</a>
</p>

<h3 align="center">Finance apps want your data. This one doesn't.</h3>

<p align="center">
We believe personal finance should actually be <em>personal</em>. No corporation should sit between you and your financial data. Securo is an open-source finance manager that runs on your own infrastructure, giving you full visibility into your accounts, spending, and habits, without surrendering a single byte to third parties. Take back control.
</p>

## Quick Start

**Linux & macOS** (uses Docker or Podman; installs Docker if neither is present):

```bash
curl -fsSL https://usesecuro.com/install.sh | bash
```

**Windows:** Install [Docker Desktop](https://www.docker.com/products/docker-desktop/), then:

```bash
git clone https://github.com/securo-finance/securo.git && cd securo
docker compose up --build
```

Open [http://localhost:3000](http://localhost:3000) and create an account. That's it.

<p align="center">
  <img src="docs/screenshot.png" width="800" alt="Securo dashboard" />
</p>

## Features

- Multi-account management with running balances
- Transaction management with search, filters, and CSV export
- File import (OFX, QIF, CAMT, CSV)
- Auto-categorization rules engine
- Recurring transactions and budgets
- Goals and savings targets with progress tracking
- Asset management with valuation tracking and growth rules
- Reports: Net Worth and Income vs Expenses with category sparklines
- Bank sync via providers (Pluggy for Brazilian banks, Enable Banking for ~2500 European PSD2 banks, SimpleFIN for US and international banks, extensible)
- Multi-currency support with automatic FX conversion
- Multi-user support with admin panel and registration controls
- Two-factor authentication (TOTP) with brute-force protection
- OIDC login support for Authentik, Pocket ID, and other standard providers
- AI Agents (optional): self-hosted LLM chat with tool-use over your data, plus a per-agent RAG knowledge base

## Bank Sync (Optional)

Add credentials for any of the supported providers to `.env`, then restart with `docker compose up`. Configure one or both — each provider auto-registers when its credentials are present.

### Pluggy — Brazilian banks

Sign up at [pluggy.ai](https://pluggy.ai) and add:

```
PLUGGY_CLIENT_ID=your-client-id
PLUGGY_CLIENT_SECRET=your-client-secret
```

### Enable Banking — European banks (PSD2)

Sign up at [enablebanking.com](https://enablebanking.com), create a Production application, and download its PEM private key. Save the PEM to `./secrets/` (gitignored), then add:

```
ENABLE_BANKING_APP_ID=your-application-id
ENABLE_BANKING_PRIVATE_KEY_FILE=/app/secrets/your-key.pem
ENABLE_BANKING_OAUTH_REDIRECT_URI=https://your-host/oauth/callback
```

The redirect URI must match exactly one of the Allowed Redirect URLs in your EB application. Production EB requires HTTPS — for local development, expose your frontend via a tunnel (ngrok, cloudflared) or use the EB sandbox.

> **Free tier — restricted mode.** Enable Banking's free plan requires you to pre-link the accounts you want to import inside the EB portal *before* connecting from Securo. If you skip that step, the connection returns no accounts and Securo will surface a banner with a link to the portal.

### SimpleFIN — US and international banks

[SimpleFIN](https://www.simplefin.org/) is a read-only open protocol. No API key needed — each connection brings its own credentials via a single-use Setup Token from the [SimpleFIN Bridge](https://bridge.simplefin.org/). Just enable the feature:

```
SIMPLEFIN_ENABLED=true
SIMPLEFIN_API_URL=https://beta-bridge.simplefin.org   # sandbox; use bridge.simplefin.org for real banks
```

Then in Securo: **Accounts → Connect Bank → SimpleFIN**, and paste the token. The [developer page](https://beta-bridge.simplefin.org/info/developers) gives out free demo tokens if you want to try it without a real bank.

### Plaid — US banks, cards and brokerages

[Plaid](https://plaid.com/) links a bank login through its Link popup and serves balances, transactions (cursor sync, up to two years of history on first link) and investment holdings. Create a team at [dashboard.plaid.com](https://dashboard.plaid.com/), copy the Client ID and the Production and Sandbox secrets, and register `{FRONTEND_URL}/plaid/oauth` under **Team Settings → API → Allowed redirect URIs** (OAuth banks such as Chase, Capital One and Bank of America refuse to open Link without it).

```
PLAID_CLIENT_ID=
PLAID_SECRET=                       # Production secret
PLAID_SANDBOX_SECRET=               # Sandbox secret (optional, for testing with Plaid's fake banks)
PLAID_ENV=production                # or sandbox; each linked Item remembers its own environment
# PLAID_OAUTH_REDIRECT_URI=         # defaults to ${FRONTEND_URL}/plaid/oauth
```

Then in Securo: **Accounts → Connect Bank → Plaid**. Reconnecting an errored connection reopens Link in update mode; disconnecting removes the Item at Plaid so it stops counting against your plan. Loan accounts (mortgages, student loans) are not imported yet.

## OIDC Login (Optional)

Securo can delegate login to any standard OIDC provider, including Authentik and Pocket ID. Create a confidential/web application in your provider and register this redirect URI:

```
https://your-securo-host/api/auth/oidc/callback
```

Then add the provider settings to `.env` and restart:

```
OIDC_ENABLED=true
OIDC_PROVIDER_NAME=Pocket ID
OIDC_DISCOVERY_URL=https://id.example.com/.well-known/openid-configuration
OIDC_CLIENT_ID=securo
OIDC_CLIENT_SECRET=your-client-secret
# Optional; defaults to ${FRONTEND_URL}/api/auth/oidc/callback
OIDC_REDIRECT_URI=https://your-securo-host/api/auth/oidc/callback
```

To require SSO-only access after OIDC is configured, set `LOCAL_AUTH_ENABLED=false`. Securo will start in this mode only when `OIDC_ENABLED=true`, `OIDC_CLIENT_ID`, and `OIDC_DISCOVERY_URL` are all configured; otherwise startup fails with a validation error instead of leaving the instance with no usable login method. The login page shows an explicit configuration error if the server reports that neither local auth nor OIDC is available. If only the optional OIDC-config request fails, the client keeps local controls available with a warning; the backend remains authoritative and still rejects them in OIDC-only mode.

With local auth disabled, Securo rejects password and passkey login, public registration, first-admin password setup, admin or workspace-invite creation of password-backed users, forgot/reset-password requests, password updates, new passkey registration or verification, and new TOTP setup or enablement. Local credential controls are hidden from login, account, setup, registration, and admin user-management screens. Existing users, password hashes, active sessions, passkeys, and TOTP configuration are not deleted; existing passkeys and TOTP can still be removed as cleanup paths. OIDC user provisioning and existing-account linking remain controlled separately by `OIDC_AUTO_REGISTER` and `OIDC_EXISTING_USER_LINK_MODE`.

On a fresh OIDC-only instance, the first account must be provisioned through OIDC. Keep `OIDC_AUTO_REGISTER=true`, enable `OIDC_SYNC_ROLES=true`, and include one of the values from `OIDC_ADMIN_ROLES` in that identity's configured roles claim so the first login becomes a Securo administrator. Do not disable OIDC auto-registration before at least one matching account exists.

New OIDC users are auto-provisioned by default (`OIDC_AUTO_REGISTER=true`) using verified email addresses. Set `OIDC_AUTO_REGISTER=false` to allow only existing Securo users whose email matches the provider claim.

### Linking existing accounts

An account that already exists in Securo (created with a password) is never linked to an OIDC identity automatically, so the first SSO login of an existing user is rejected by default. `OIDC_EXISTING_USER_LINK_MODE` controls that:

```
OIDC_EXISTING_USER_LINK_MODE=disabled
```

| Value | Behavior |
|-------|----------|
| `disabled` (default) | Never link. Existing accounts must keep using password login. |
| `verified_email` | Link the existing account when the provider sends `email_verified=true` for the same email. |
| `email` | Link on a matching email alone, even without `email_verified`. |

Use `verified_email` to move existing users to SSO without recreating their accounts and data. Only pick `email` if you trust your provider to own every address it asserts, since anyone able to set an email there could claim the matching Securo account. An OIDC identity already linked to another account is always rejected, in every mode.

### Optional OIDC role sync

Securo can also synchronize provider roles/groups into its built-in permissions when `OIDC_SYNC_ROLES=true`. The default claim is `groups`, which works well with Authentik group mappings and Pocket ID role/group assignments.

```
OIDC_SYNC_ROLES=true
OIDC_ROLES_CLAIM=groups
OIDC_ADMIN_ROLES=securo-admins
OIDC_WORKSPACE_ROLE_MAP={"securo-owners":"owner","securo-editors":"editor","securo-viewers":"viewer"}
```

`OIDC_ADMIN_ROLES` grants or revokes Securo admin (`is_superuser`) on each OIDC login. `OIDC_WORKSPACE_ROLE_MAP` maps provider roles/groups to the user's Personal workspace role (`owner`, `editor`, or `viewer`); if multiple mapped roles are present, Securo applies the highest permission. Leave `OIDC_SYNC_ROLES=false` to keep all Securo roles managed locally.

## Passkeys (Optional)

Sign in with Touch ID, Face ID, Windows Hello, or a security key. Passkeys are on by default and need no configuration: they follow whatever address you open Securo on.

Two rules come from the WebAuthn standard itself, and no setting can work around them:

- **An IP address is never valid.** `http://192.168.1.10:3000` cannot register passkeys.
- **Plain HTTP is never valid, except on `localhost`.**

So use passkeys on `http://localhost:3000`, or put Securo on a domain behind an HTTPS reverse proxy. When serving from a domain, point `FRONTEND_URL` at it (this also covers CORS and OAuth callbacks):

```
FRONTEND_URL=https://securo.example.com
```

To pin passkeys to one domain, set `WEBAUTHN_RP_ID` (use the parent domain if you reach Securo on several subdomains). Otherwise Securo follows the browser, and requests from an unusable address get an explanation in the UI instead of a silent failure.

## Exchange Rates (Optional)

For automatic currency conversion, add a free [Open Exchange Rates](https://openexchangerates.org/) key to `.env`:

```
OPENEXCHANGERATES_APP_ID=your-app-id
```

Rates are fetched on-demand when foreign-currency transactions are created. Without a key, cross-currency amounts default to a 1:1 fallback rate with a visual warning.

## AI Agents (Optional)

Self-hosted AI assistants over your Securo data — multi-provider (OpenAI, Anthropic, Ollama, OpenAI-compatible), tool-use via MCP, per-agent RAG knowledge base, ⌘J global chat panel.

Add to `.env`:

```
AGENTS_ENABLED=true
COMPOSE_PROFILES=agents
```

Then `docker compose up -d`. Settings → AI Agents to add a provider connection. Off by default; zero cost when off.

### Without Docker

`COMPOSE_PROFILES=agents` only tells Docker Compose to start the extra `mcp-server` container, so on a bare-metal or LXC install set `AGENTS_ENABLED=true` alone. The built-in MCP server is a plain uvicorn app in the same virtualenv; run it next to the API and point the backend at it:

```bash
# alongside the API/worker/beat processes
uvicorn mcp_server.main:app --host 127.0.0.1 --port 8765
```

```
AGENTS_ENABLED=true
AGENTS_BUILTIN_MCP_URL=http://127.0.0.1:8765/mcp
```

Without that server the agents still chat, but they have no tools and cannot read your data. The backend log says which MCP server it failed to reach.

### Local models with Ollama on another machine

Securo talks to Ollama's native API, so the model server can live on any box on your LAN (a Mac with a GPU, a workstation) while Securo runs on small hosts. On the Ollama machine bind it to the network (`launchctl setenv OLLAMA_HOST 0.0.0.0:11434` on macOS, `Environment="OLLAMA_HOST=0.0.0.0"` in the systemd unit on Linux) and keep the port LAN-only — Ollama has no authentication. Then point Securo at it: `AGENTS_OLLAMA_BASE_URL=http://<ollama-host>:11434` (`http://host.docker.internal:11434` when Ollama runs on the Docker host), or add a connection of kind **Ollama** under `/agents/connections`.

Settings that matter for local models (all optional):

| Variable | Default | Why |
|----------|---------|-----|
| `AGENTS_OLLAMA_NUM_CTX` | `0` (server default) | Context window sent as `options.num_ctx`. The tool schemas alone are ~7K tokens, so tool use needs `32768` (or `16384` when memory is tight). |
| `AGENTS_OLLAMA_THINK` | unset | `false` / `true` / `low` / `medium` / `high`, sent as `think`. gpt-oss accepts levels only; Qwen-class models need `false` to stop emitting thinking. Thinking is never streamed to the UI. |
| `AGENTS_OLLAMA_KEEP_ALIVE` | unset | How long the model stays loaded after a request, e.g. `30m` or `-1`. |
| `AGENTS_LLM_TIMEOUT_SECONDS` | `120` | Read timeout of every provider's chat stream. Large local models need `300`: the first token after idle includes the model load. |
| `AGENTS_EMBEDDING_PROVIDER=ollama` + `AGENTS_EMBEDDING_OLLAMA_BASE_URL` + `AGENTS_EMBEDDING_MODEL` | native fastembed | Knowledge-base embeddings from Ollama (e.g. `qwen3-embedding:0.6b`). Shorter vectors are zero-padded to the 1536-dim column, which leaves cosine similarity unchanged; re-upload documents after switching models. |
| `AGENTS_KNOWLEDGE_INGEST_INLINE` | `false` | Chunk and embed uploads inside the API process instead of the Celery worker, so the knowledge files need no volume shared with the worker. |
| `AGENTS_PINNED_CONTEXT_MAX_CHARS` | `6000` | Documents pinned in an agent's Knowledge tab are injected into every conversation, up to this many characters. |
| `AGENTS_DIGEST_ENABLED`, `AGENTS_DIGEST_HOUR` | `false`, `7` | Weekly (Monday) and monthly (1st) finance reviews written by each workspace's default agent into its own history, at that local hour. |
| `AGENTS_MAX_TOOL_ITERATIONS` | `10` | Ceiling of the free-form tool loop per message (per agent: `extra.max_tool_iterations`, clamped 1..50). Hitting it persists a short, localized stop message instead of an empty bubble. |
| `AGENTS_TOOL_RESULT_MAX_CHARS` | `4000` | What the model reads back from a tool: a dict's biggest list is cut to the longest prefix that fits and marked `truncated`/`omitted_items` (the full payload is still persisted for cards and debugging). `0` disables the cap. |
| `AGENTS_PROMPT_BUDGET_TOKENS` | `20000` | Oldest whole turns are dropped from the prompt until system prompts + history fit (≈4 chars/token). Sized for a 32K window minus tool schemas and generation. `0` disables. |
| `AGENTS_SHOW_TOOL_COMMENTARY` | `false` | Text a model emits in the same turn as its tool calls is planning commentary: hidden from the UI (`text_discard`), kept trimmed on the persisted turn, never fed back. `true` restores the old behaviour. |
| `AGENTS_GUIDED_MODE`, `AGENTS_GUIDED_MIN_CONFIDENCE` | `true`, `0.7` | Route each message through a small schema-constrained intent classifier first and answer the known intents from code (see Guided mode). Below the confidence threshold the ordinary tool loop runs. |
| `AGENTS_WORKFLOW_AUTO_APPLY` | `false` | Lets `/categorize apply=true` (or `extra.auto_apply_rules` on the agent) create and apply rules with confidence ≥ 0.90 in code instead of proposing them. Off: every rule is a card you apply. |
| `AGENTS_WORKFLOW_MAX_LLM_CALLS`, `AGENTS_WORKFLOW_MAX_SECONDS` | `8`, `240` | Budget of one workflow run; merchants left when it runs out are listed, never guessed. |
| `AGENTS_CLASSIFIER_BACKEND` | `ollama` | Model behind the categorization step: `ollama` (schema-constrained call through the agent's connection) or `kev` (a local Kev decision-model server; falls back to Ollama when unreachable). |
| `AGENTS_KEV_BASE_URL`, `AGENTS_KEV_MODEL`, `AGENTS_KEV_TIMEOUT_SECONDS` | unset, `kev-latest`, `20` | Where the Kev server answers `POST /v1/systemone` (for example `http://192.168.86.22:8009`), the model name, and the per-request timeout. |

The chat stream sends an SSE comment every 15 s of silence, so reverse proxies and CDNs with idle limits (Cloudflare closes at 100 s) keep slow first tokens alive.

### Finance analyst

A ready-made agent for personal finance analysis over your data, with a prompt that makes it fetch every number through tools and treat transfer-style categories as movements rather than income or spending. Seed it once (idempotent):

```bash
python -m app.agents.scripts.seed_finance_analyst --email you@example.com --model gpt-oss:20b --base-url http://ollama:11434
```

It creates the Ollama connection if needed, the agent, and a 20-tool whitelist including the finance tools: `get_transactions_summary` (income, expense, net, invested, savings rate per period), `get_money_map` (where money went), `list_uncategorized_merchants` + `list_rules` (categorization proposals that become rules on Apply), `get_holdings`, and `fire_projection` (financial-independence math from your own trailing-12-month figures). Pin your household conventions document in the agent's Knowledge tab so every conversation starts from it.

### Workflows

Multi-step jobs run as code, not as a chain of model calls. A workflow loads its data in-process, decides everything it can deterministically, asks the model only closed questions (schema-constrained JSON, validated before use), and writes its own reply. The first one is the **categorization review**:

- `/categorize` (alias `/review`, params `from=YYYY-MM-DD to=YYYY-MM-DD limit=60`) reads the uncategorized merchants, your categories and rules; classifies transfers between your own accounts and credit-card payments from the descriptions and the accounts' last four digits; skips merchants an existing rule already covers; sends the rest to the model in batches of 12 with the category list and your pinned conventions; checks every answer (allowed category, money-in vs money-out, confidence); and emits one proposal card per confident merchant plus a summary with a "needs your call" list. Cards apply one by one or with **Apply all**.
- The model can trigger the same workflow with the `workflow__categorize` tool (whitelisted per agent like any tool); the workflow then ends the turn itself.
- `/ask <question>` forces the plain tool loop for one message.

### Guided mode

Every ordinary message is first classified by a short, schema-constrained router call (intent + period slots from a closed grammar; dates are resolved in code, in your timezone). Six intents are answered without the tool loop: period comparison, spending breakdown, money map, net worth trend, FIRE progress and holdings. Code fetches the figures, renders the table and the chart, and the model only writes a short narration that is checked number by number against those figures (a narration that invents a number is regenerated once, then replaced by a templated sentence). Categorization requests go to the workflow above; anything the router is unsure about, follow-ups, specific merchants and every request to change data fall through to the tool loop. Set `extra.mode` to `"freeform"` on an agent to opt it out, or `AGENTS_GUIDED_MODE=false` globally.

## Tech Stack

| Layer | Stack |
|-------|-------|
| Backend | FastAPI, SQLAlchemy, Alembic, Celery |
| Frontend | React, TypeScript, Vite, Tailwind CSS |
| Database | PostgreSQL |
| Queue | Redis + Celery |

## AI-Assisted Development

Parts of this codebase were built with help of AI. All code is human-reviewed and no data leaves your environment.

Contributing with AI is welcome. We review the author, not the tool: whatever wrote the diff, you own its quality, its fit with where Securo is going, and everything that happens after it merges. See [Using AI](CONTRIBUTING.md#using-ai).

## Development

```bash
# Run backend tests (from backend/, needs Python 3.11+; same as CI)
cd backend
pip install -e ".[dev]"   # first time only — installs pytest and dev deps
pytest

# Rebuild after dependency changes
docker compose up --build
```

If you've [mise](https://mise.jdx.dev/) installed, you can install backend/frontend directly with it:

```
# Install the Python version specified in .python-version,
# and create a project virtual environment using that Python.
# Install all tools and dependencies (include Python with dedicated venv)
mise //...:install

# Install only backend tools/deps
mise backend:install

# Run backend tests
mise backend:test

# Install frontend dependencies
mise frontend:install

# Run frontend linting
mise frontend:lint

# Run frontend build
mise frontend:build
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Not sure where to start, or want to talk something through first? [Book 15 minutes](https://cal.com/tassio/15min) — no agenda needed. Something broken, an idea, or just what you think of Securo, all welcome.

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE).

This means you can freely use, modify, and distribute this software, but any modifications — including when used as a network service (SaaS) — must also be released under the AGPL-3.0.
