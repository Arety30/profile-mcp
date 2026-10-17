# profile-mcp

profile-mcp is a small Model Context Protocol (MCP) server, written by Syed Abrar Ali, that keeps one shared "profile" of its owner: short, durable facts such as which devices he uses, what he is building, how he likes tools to behave and what constraints he works under. Any MCP client that connects to it (over stdio locally, or over streamable HTTP from a remote connector) reads and writes the same JSON store, so a fact learned in one client is immediately visible in every other. Every write is attributed to the client that made it and logged, so entries can be compared, corrected or removed later. A second module adds an optional LinkedIn integration (OAuth login, read basic profile, publish and delete feed posts) behind the same server.

## Where it is used

In Abrar's setup the server runs in two forms at once: as a user-scoped stdio MCP server registered in a local command-line assistant, and as a long-running HTTP service bound to localhost and published through a reverse proxy under a secret path so that a web-based assistant's "connector" can reach it. Both forms point at the same file under `~/.local/share/profile-mcp/`, which is the whole point: it is the single memory shared by otherwise separate tools. The LinkedIn tools let the same assistants draft a post, show it for approval, and publish it to his feed.

## How it works

### Components

| File | Role |
|---|---|
| `server.py` | FastMCP server: profile store, seven profile tools, five LinkedIn tools, two HTTP routes for the OAuth flow, CLI entry point (`--http`, `--port`, `--debug`) |
| `linkedin.py` | LinkedIn OAuth 2.0 (OpenID Connect + "Share on LinkedIn"), token storage, `userinfo`, `POST/DELETE /rest/posts` calls using only the standard library |
| `README.md` | Short operator notes |

### Profile store

- Location: `~/.local/share/profile-mcp/profile.json` (override with `PROFILE_STORE`). File mode 0600.
- Shape: `{"owner": "Syed Abrar Ali", "facts": [...], "log": [...]}`. Each fact has `id` (6 hex chars), `section`, `text`, `tags`, `by`, `created`, `updated` and, after edits, a `history` list of up to 10 previous versions.
- Sections are fixed: `identity, accounts, devices, projects, skills, preferences, constraints, interests, goals, history, other`. An unknown section is rejected.
- Writes take an exclusive `fcntl` lock on a sidecar `.lock` file, then write to a temp file and `os.replace` it, so concurrent clients never see a half-written file.
- The change log keeps the last 500 entries (`at`, `by`, `action`, `id`, truncated `text`).
- Timestamps are local time at a fixed UTC+3 offset.

### Decision rules in the tools

- `add_fact` refuses empty text, text over 600 characters, and anything matching a secret-shaped regex (common API-key prefixes, JWT-like strings, 13-19 digit card-number-like sequences, `password:`/`password=`, and one blocked e-mail address that must never be stored). It also de-duplicates on case-insensitive text and returns `already_present` instead of adding a copy.
- `update_fact` changes only the fields passed, bumps `updated`, re-attributes `by`, and pushes the old version onto `history`.
- `delete_fact` and both LinkedIn write tools are two-step: without `confirm=true` they return `needs_confirm` plus a preview, so the calling client has to show the user first.
- Attribution (`by`) comes from an explicit `source` argument, else from the MCP `clientInfo.name`, else from a per-transport default that is set at startup (stdio and HTTP each have their own default label).

### Tools

Profile: `get_profile(section, format=markdown|json)`, `list_sections`, `search_facts(query, limit)`, `add_fact(text, section, tags, source)`, `update_fact(id, ...)`, `delete_fact(id, confirm)`, `recent_changes(limit)`.

LinkedIn: `linkedin_status`, `linkedin_whoami`, `linkedin_post(text, link_url, link_title, link_description, visibility, confirm)`, `linkedin_delete_post(post_urn, confirm)`.

### LinkedIn flow

1. `GET /linkedin/login?key=<LINKEDIN_LOGIN_KEY>` checks the key with a constant-time compare, creates a random `state` (kept in memory for 15 minutes) and redirects to LinkedIn with scopes `openid profile email w_member_social`.
2. `GET /linkedin/callback` exchanges the code for an access token, calls `/v2/userinfo`, and stores token + identity in `~/.local/share/profile-mcp/linkedin.json` (0600). Tokens last 60 days; LinkedIn issues no refresh token to ordinary apps, so the login page is revisited when `linkedin_status` reports `expired`.
3. `post()` builds a Posts API body (`author = urn:li:person:<sub>`, `commentary`, `visibility`, optional article card) with `LinkedIn-Version: 202506`, and returns the post URN taken from the `x-restli-id` response header plus a feed URL. Text is capped at 3000 characters; visibility must be `PUBLIC` or `CONNECTIONS`.

### HTTP mode details

With `--http` the server listens on `127.0.0.1:<port>` at path `/mcp` using streamable HTTP in **stateless, JSON-response** mode, because some remote connectors probe without an `Mcp-Session-Id` and a session-based server would answer 400. Allowed `Host`/`Origin` values are set in code; change them to your own public hostname when deploying behind a proxy.

## Setup

Requirements: Python 3.11+, the `mcp` package (FastMCP) and `starlette` (pulled in by `mcp`). Linux or macOS (uses `fcntl`).

```bash
git clone https://github.com/USER/profile-mcp ~/profile-mcp
cd ~/profile-mcp
python -m venv .venv && . .venv/bin/activate
pip install "mcp[cli]"
```

Optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `PROFILE_STORE` | `~/.local/share/profile-mcp/profile.json` | Profile JSON path |
| `LINKEDIN_STORE` | `~/.local/share/profile-mcp/linkedin.json` | LinkedIn token path |

LinkedIn credentials are read from `~/.config/linkedin/env` (plain `KEY=value` lines):

```
LINKEDIN_CLIENT_ID=your-client-id
LINKEDIN_CLIENT_SECRET=your-client-secret
LINKEDIN_LOGIN_KEY=a-long-random-string
LINKEDIN_REDIRECT_URI=https://example.com/linkedin/callback
```

The redirect URI must match the one registered in the LinkedIn developer app, and the app needs the "Sign In with LinkedIn using OpenID Connect" and "Share on LinkedIn" products.

## Usage

Stdio (for a local MCP client):

```bash
python ~/profile-mcp/server.py
```

HTTP (for a remote connector, behind your own TLS reverse proxy):

```bash
python ~/profile-mcp/server.py --http --port 8981
# proxy  https://example.com/profile/<secret>/  ->  http://127.0.0.1:8981/mcp
```

Typical interactions from a client:

- "What do you know about me?" → `get_profile()` returns a markdown list grouped by section with fact ids.
- "I moved my mail server to the Pi" → `search_facts("mail server")`, then `update_fact(id, text=...)`.
- "Forget that I use editor X" → `delete_fact(id)` returns `needs_confirm`; after the user agrees, `delete_fact(id, confirm=true)`.
- Publishing: `linkedin_post(text)` returns a preview; `linkedin_post(text, confirm=true)` publishes and logs the URN.

To connect LinkedIn, open `https://example.com/linkedin/login?key=<LINKEDIN_LOGIN_KEY>` once in a browser; `linkedin_status` then shows the account name and days left on the token.

## Project structure

```
profile-mcp/
├── server.py      # FastMCP server, profile tools, LinkedIn tools, OAuth routes, CLI
├── linkedin.py    # LinkedIn OAuth + Posts API client (stdlib only)
└── README.md
```

## Limitations and notes

- Single-user by design: there is one owner and one profile file; there is no authentication inside the server itself. In HTTP mode, protection relies entirely on binding to localhost and on the secret path or auth configured in the reverse proxy.
- OAuth `state` values live in process memory, so a restart between the login redirect and the callback invalidates the login attempt.
- The LinkedIn API used here can read the member's basic profile and create or delete posts; it cannot edit profile sections (headline, about, experience).
- `search_facts` is a plain case-insensitive substring match over text, tags and section, not full-text or fuzzy search.
- The allowed-hosts and allowed-origins lists for HTTP mode are hard-coded in `server.py` and must be edited for a different deployment.
