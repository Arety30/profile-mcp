"""
Profile MCP — one shared profile of Abrar that ChatGPT and Claude both read and write.

Both assistants connect to the same store (~/.local/share/profile-mcp/profile.json), so a fact learned
in one shows up in the other. Every fact records which assistant wrote it and when, so entries can be
compared, corrected or removed later.

Tools: get_profile, list_sections, search_facts, add_fact, update_fact, delete_fact, recent_changes.

    python server.py                       -> stdio (Claude Code)
    python server.py --http [--port 8981]  -> streamable HTTP at /mcp on 127.0.0.1
                                              (published as https://example.com/profile/<token> for ChatGPT)
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import linkedin

STORE = Path(os.environ.get("PROFILE_STORE", Path.home() / ".local/share/profile-mcp/profile.json"))
LOCK = STORE.with_suffix(".lock")
QATAR = timezone(timedelta(hours=3))
DEFAULT_SOURCE = "unknown"  # set in main: stdio clients are Claude Code; the HTTP endpoint is used by ChatGPT (stateless, so clientInfo is not kept)

SECTIONS = {
    "identity": "Name, age, where he lives, languages, how he likes to be addressed",
    "accounts": "Usernames and which email/account is used where (never passwords or tokens)",
    "devices": "Laptop, Pi, VMs, phones and what runs on each",
    "projects": "Things he is building, their status and goals",
    "skills": "What he knows well, what he is learning",
    "preferences": "How he likes assistants to work, formatting, tone, do/don't rules",
    "constraints": "Budget, time, hardware or policy limits that shape decisions",
    "interests": "Topics, hobbies, what he enjoys",
    "goals": "Short- and long-term aims",
    "history": "Dated events and decisions worth remembering",
    "other": "Anything that fits nowhere else",
}

mcp = FastMCP(
    "profile",
    instructions=(
        "Shared profile of Abrar (Syed Abrar Ali) that both ChatGPT and Claude read and write. "
        "Call get_profile at the start of a conversation when you need background about him. "
        "When he tells you something durable about himself, his setup, projects or preferences, store it with add_fact "
        "(one fact per call, short, in third person, with a section). If he corrects an existing fact, use update_fact "
        "on that fact's id instead of adding a duplicate. Never store passwords, API keys, tokens, card numbers or the "
        "email user@example.com (that is the subscription owner, not Abrar). Delete only what Abrar asks to delete. "
        "LinkedIn: linkedin_post publishes to Abrar's public LinkedIn feed — always show him the exact final text and get his "
        "explicit OK first, then call with confirm=true. LinkedIn's API cannot edit profile sections (headline, about, experience); "
        "if he asks for that, say so and offer a post instead. Never use the LinkedIn tools when linkedin_status says not connected."
    ),
)


# ------------------------------------------------------------------ storage ----
def _now() -> str:
    return datetime.now(QATAR).strftime("%Y-%m-%d %H:%M %z")


def _load() -> dict[str, Any]:
    if not STORE.exists():
        return {"owner": "Syed Abrar Ali", "facts": [], "log": []}
    return json.loads(STORE.read_text() or "{}") or {"owner": "Syed Abrar Ali", "facts": [], "log": []}


def _save(data: dict[str, Any]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STORE.parent, prefix=".profile-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE)


class _locked:
    def __enter__(self):
        STORE.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(LOCK, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        self.data = _load()
        return self.data

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                _save(self.data)
        finally:
            fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()


def _who(ctx: Context | None, explicit: str) -> str:
    if explicit:
        return explicit
    try:
        name = (ctx.session.client_params.clientInfo.name or "").lower()  # type: ignore[union-attr]
    except Exception:
        name = ""
    if "claude" in name:
        return "claude"
    if "openai" in name or "chatgpt" in name:
        return "chatgpt"
    return DEFAULT_SOURCE  # generic SDK names like "mcp" say nothing; fall back to the transport's default


SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|nvapi-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|\b(?:\d[ -]?){13,19}\b|someone@yahoo\.com|password\s*[:=])",
    re.I,
)


def _check(text: str) -> None:
    if not text or not text.strip():
        raise ValueError("fact text is empty")
    if len(text) > 600:
        raise ValueError("keep each fact under 600 characters; split it into several facts")
    if SECRET_RE.search(text):
        raise ValueError("this looks like a secret, card number or the subscription owner's email; not stored")


def _section(s: str) -> str:
    s = (s or "other").strip().lower()
    if s not in SECTIONS:
        raise ValueError(f"unknown section '{s}'; use one of: {', '.join(SECTIONS)}")
    return s


def _log(data: dict[str, Any], action: str, fact: dict[str, Any], by: str) -> None:
    data.setdefault("log", []).append({"at": _now(), "by": by, "action": action, "id": fact["id"], "text": fact["text"][:120]})
    data["log"] = data["log"][-500:]


# -------------------------------------------------------------------- tools ----
@mcp.tool()
def get_profile(section: str = "", format: str = "markdown") -> Any:
    """Read the whole shared profile (or one section) as markdown, or as JSON with fact ids (format='json').
    Call this first when you need background about Abrar."""
    data = _load()
    facts = data.get("facts", [])
    if section:
        sec = _section(section)
        facts = [f for f in facts if f["section"] == sec]
    if format == "json":
        return {"owner": data.get("owner"), "count": len(facts), "facts": facts}
    out = [f"# Profile: {data.get('owner', 'Abrar')}", f"_{len(facts)} facts; times are Qatar (UTC+3)._", ""]
    for sec in SECTIONS:
        rows = [f for f in facts if f["section"] == sec]
        if not rows:
            continue
        out.append(f"## {sec}")
        for f in rows:
            tags = f" [{', '.join(f['tags'])}]" if f.get("tags") else ""
            out.append(f"- {f['text']}{tags}  (id {f['id']}, by {f['by']}, {f['updated']})")
        out.append("")
    if len(out) == 3:
        out.append("(empty — nothing stored yet)")
    return "\n".join(out)


@mcp.tool()
def list_sections() -> dict[str, Any]:
    """List the profile sections, what belongs in each, and how many facts each holds."""
    facts = _load().get("facts", [])
    return {s: {"about": d, "count": sum(1 for f in facts if f["section"] == s)} for s, d in SECTIONS.items()}


@mcp.tool()
def search_facts(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Case-insensitive search over fact text, tags and section. Returns matching facts with ids."""
    q = query.lower().strip()
    facts = _load().get("facts", [])
    hits = [f for f in facts if q in f["text"].lower() or q in f["section"] or any(q in t.lower() for t in f.get("tags", []))]
    return hits[: max(1, min(limit, 100))]


@mcp.tool()
def add_fact(text: str, section: str = "other", tags: list[str] | None = None, source: str = "", ctx: Context = None) -> dict[str, Any]:
    """Add one durable fact about Abrar (short, third person, e.g. "Lives in Qatar (UTC+3)").
    section: one of identity, accounts, devices, projects, skills, preferences, constraints, interests, goals, history, other.
    source: who learned it — normally left blank (detected: 'chatgpt' or 'claude'). Duplicates (same text) are not re-added."""
    _check(text)
    sec = _section(section)
    by = _who(ctx, source)
    with _locked() as data:
        for f in data["facts"]:
            if f["text"].strip().lower() == text.strip().lower():
                return {"status": "already_present", "fact": f}
        fact = {
            "id": secrets.token_hex(3),
            "section": sec,
            "text": text.strip(),
            "tags": sorted({t.strip().lower() for t in (tags or []) if t.strip()}),
            "by": by,
            "created": _now(),
            "updated": _now(),
        }
        data["facts"].append(fact)
        _log(data, "add", fact, by)
    return {"status": "added", "fact": fact}


@mcp.tool()
def update_fact(id: str, text: str = "", section: str = "", tags: list[str] | None = None, source: str = "", ctx: Context = None) -> dict[str, Any]:
    """Correct an existing fact by id (from get_profile format='json' or search_facts). Only the fields you pass change.
    Use this instead of add_fact when Abrar corrects something already stored."""
    if text:
        _check(text)
    by = _who(ctx, source)
    with _locked() as data:
        for f in data["facts"]:
            if f["id"] == id:
                old = dict(f)
                if text:
                    f["text"] = text.strip()
                if section:
                    f["section"] = _section(section)
                if tags is not None:
                    f["tags"] = sorted({t.strip().lower() for t in tags if t.strip()})
                f["updated"] = _now()
                f["by"] = by
                f.setdefault("history", []).append({"at": old["updated"], "by": old["by"], "text": old["text"]})
                f["history"] = f["history"][-10:]
                _log(data, "update", f, by)
                return {"status": "updated", "fact": f}
    raise ValueError(f"no fact with id {id}")


@mcp.tool()
def delete_fact(id: str, confirm: bool = False, source: str = "", ctx: Context = None) -> dict[str, Any]:
    """Remove a fact permanently. Only after Abrar clearly asked for that fact to go; then call with confirm=true."""
    by = _who(ctx, source)
    with _locked() as data:
        for i, f in enumerate(data["facts"]):
            if f["id"] == id:
                if not confirm:
                    return {"status": "needs_confirm", "fact": f, "hint": "ask Abrar, then call again with confirm=true"}
                data["facts"].pop(i)
                _log(data, "delete", f, by)
                return {"status": "deleted", "fact": f}
    raise ValueError(f"no fact with id {id}")


@mcp.tool()
def recent_changes(limit: int = 30) -> list[dict[str, Any]]:
    """Show the latest additions, edits and deletions and which assistant made them (newest first)."""
    return list(reversed(_load().get("log", [])))[: max(1, min(limit, 200))]


# ----------------------------------------------------------------- LinkedIn ----
@mcp.tool()
def linkedin_status() -> dict[str, Any]:
    """Is LinkedIn connected? Shows the account, days left on the token, the login page, and what the API can/cannot do."""
    return linkedin.status()


@mcp.tool()
def linkedin_whoami() -> dict[str, Any]:
    """Read Abrar's basic LinkedIn profile (name, email, picture) via OpenID Connect."""
    return linkedin.whoami()


@mcp.tool()
def linkedin_post(text: str, link_url: str = "", link_title: str = "", link_description: str = "",
                  visibility: str = "PUBLIC", confirm: bool = False, source: str = "", ctx: Context = None) -> dict[str, Any]:
    """Publish a post to Abrar's LinkedIn feed (max 3000 chars; optional link card). PUBLIC or CONNECTIONS.
    Show Abrar the exact text first; only after his explicit OK call again with confirm=true. Returns the post URN and URL."""
    if not confirm:
        return {"status": "needs_confirm", "preview": text, "link_url": link_url, "visibility": visibility,
                "hint": "show this to Abrar; call again with confirm=true once he approves"}
    res = linkedin.post(text, link_url, link_title, link_description, visibility)
    by = _who(ctx, source)
    with _locked() as data:
        data.setdefault("log", []).append({"at": _now(), "by": by, "action": "linkedin_post", "id": res.get("post_urn", ""), "text": text[:120]})
    return res


@mcp.tool()
def linkedin_delete_post(post_urn: str, confirm: bool = False, source: str = "", ctx: Context = None) -> dict[str, Any]:
    """Delete one of Abrar's LinkedIn posts by URN (from linkedin_post's result). Only when he asks; confirm=true required."""
    if not confirm:
        return {"status": "needs_confirm", "post_urn": post_urn}
    res = linkedin.delete_post(post_urn)
    by = _who(ctx, source)
    with _locked() as data:
        data.setdefault("log", []).append({"at": _now(), "by": by, "action": "linkedin_delete_post", "id": post_urn, "text": ""})
    return res


@mcp.custom_route("/linkedin/login", methods=["GET"])
async def _li_login(request: Request):
    url = linkedin.login_url(request.query_params.get("key", ""))
    if not url:
        return PlainTextResponse("LinkedIn app not configured or wrong key", status_code=403)
    return RedirectResponse(url)


@mcp.custom_route("/linkedin/callback", methods=["GET"])
async def _li_callback(request: Request):
    q = request.query_params
    if q.get("error"):
        return HTMLResponse(f"<h2>LinkedIn refused</h2><p>{q.get('error')}: {q.get('error_description','')}</p>", status_code=400)
    try:
        d = linkedin.finish_login(q.get("code", ""), q.get("state", ""))
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f"<h2>Login failed</h2><pre>{str(e)[:800]}</pre>", status_code=400)
    return HTMLResponse(f"<h2>LinkedIn connected</h2><p>Signed in as <b>{d.get('name')}</b> ({d.get('email')}). "
                        f"The token lasts 60 days. You can close this tab.</p>")


# --------------------------------------------------------------------- main ----
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true")
    ap.add_argument("--port", type=int, default=8981)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    if not a.http:
        DEFAULT_SOURCE = "claude"
        mcp.run(transport="stdio")
        raise SystemExit
    DEFAULT_SOURCE = "chatgpt"
    mcp.settings.host, mcp.settings.port, mcp.settings.streamable_http_path = "127.0.0.1", a.port, "/mcp"
    # Stateless: ChatGPT's connector probe sends requests without an Mcp-Session-Id, which a
    # session-based server rejects with 400 "Missing session ID". Each request is self-contained here.
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.log_level = "DEBUG" if a.debug else "INFO"
    try:
        mcp.settings.transport_security.allowed_hosts = ["example.com", "HOST.ts.net", "HOST.ts.net:*", "127.0.0.1:*", "localhost:*"]
        mcp.settings.transport_security.allowed_origins = ["https://example.com", "https://HOST.ts.net", "https://HOST.ts.net:8443", "https://chatgpt.com", "https://chat.openai.com"]
    except Exception:
        pass
    mcp.run(transport="streamable-http")
