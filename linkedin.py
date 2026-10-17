"""
LinkedIn integration for the profile connector (official APIs only).

- OAuth 2.0 "Sign In with LinkedIn (OpenID Connect)" + "Share on LinkedIn":
      GET /linkedin/login?key=<LINKEDIN_LOGIN_KEY>   -> redirects to LinkedIn
      GET /linkedin/callback                          -> stores the token, shows a done page
- Tokens live in ~/.local/share/profile-mcp/linkedin.json (0600). Access tokens last 60 days; LinkedIn does
  not issue refresh tokens to ordinary apps, so the login page is revisited when `linkedin_status` says expired.
- Credentials in ~/.config/linkedin/env: LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET, LINKEDIN_LOGIN_KEY,
  LINKEDIN_REDIRECT_URI (default https://example.com/linkedin/callback).
The API can read the member's basic profile and publish/delete posts. It cannot edit profile sections.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ENV = Path.home() / ".config/linkedin/env"
STORE = Path(os.environ.get("LINKEDIN_STORE", Path.home() / ".local/share/profile-mcp/linkedin.json"))
API_VERSION = "202506"
SCOPES = "openid profile email w_member_social"
_states: dict[str, float] = {}


def _env() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    cfg.setdefault("LINKEDIN_REDIRECT_URI", "https://example.com/linkedin/callback")
    return cfg


def _load() -> dict[str, Any]:
    return json.loads(STORE.read_text()) if STORE.exists() else {}


def _save(d: dict[str, Any]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(d, indent=1))
    os.chmod(STORE, 0o600)


def _http(method: str, url: str, token: str | None = None, body: Any = None, form: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:
    headers = {"Accept": "application/json", "User-Agent": "arety-profile-connector/1.0"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
        headers["LinkedIn-Version"] = API_VERSION
        headers["X-Restli-Protocol-Version"] = "2.0.0"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read().decode() or ""
            return r.status, dict(r.headers), (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw[:800]
        return e.code, dict(e.headers), parsed


# ------------------------------------------------------------------ OAuth ----
def login_url(key: str) -> str | None:
    cfg = _env()
    if not cfg.get("LINKEDIN_CLIENT_ID") or not cfg.get("LINKEDIN_LOGIN_KEY"):
        return None
    if not secrets.compare_digest(key or "", cfg["LINKEDIN_LOGIN_KEY"]):
        return None
    state = secrets.token_urlsafe(24)
    now = time.time()
    for s, t in list(_states.items()):
        if now - t > 900:
            _states.pop(s, None)
    _states[state] = now
    q = {"response_type": "code", "client_id": cfg["LINKEDIN_CLIENT_ID"], "redirect_uri": cfg["LINKEDIN_REDIRECT_URI"], "state": state, "scope": SCOPES}
    return "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(q)


def finish_login(code: str, state: str) -> dict[str, Any]:
    cfg = _env()
    if state not in _states:
        raise ValueError("unknown or expired login state; start again from the login link")
    _states.pop(state, None)
    status, _, tok = _http("POST", "https://www.linkedin.com/oauth/v2/accessToken", form={
        "grant_type": "authorization_code", "code": code, "redirect_uri": cfg["LINKEDIN_REDIRECT_URI"],
        "client_id": cfg["LINKEDIN_CLIENT_ID"], "client_secret": cfg["LINKEDIN_CLIENT_SECRET"]})
    if status != 200 or not isinstance(tok, dict) or "access_token" not in tok:
        raise ValueError(f"token exchange failed ({status}): {tok}")
    status, _, me = _http("GET", "https://api.linkedin.com/v2/userinfo", token=tok["access_token"])
    if status != 200 or not isinstance(me, dict):
        raise ValueError(f"userinfo failed ({status}): {me}")
    d = {"access_token": tok["access_token"], "expires_at": time.time() + int(tok.get("expires_in", 5184000)),
         "scope": tok.get("scope", SCOPES), "sub": me.get("sub"), "name": me.get("name"), "email": me.get("email"),
         "picture": me.get("picture"), "logged_in": time.strftime("%Y-%m-%d %H:%M")}
    _save(d)
    return d


# ------------------------------------------------------------------ tools ----
def _auth() -> dict[str, Any]:
    d = _load()
    if not d.get("access_token"):
        raise ValueError("LinkedIn is not connected yet. Abrar must open the login link once (see linkedin_status).")
    if time.time() > d.get("expires_at", 0):
        raise ValueError("LinkedIn token expired (they last 60 days). Abrar must open the login link again.")
    return d


def status() -> dict[str, Any]:
    cfg = _env()
    d = _load()
    base = cfg["LINKEDIN_REDIRECT_URI"].rsplit("/", 1)[0]
    out: dict[str, Any] = {
        "app_configured": bool(cfg.get("LINKEDIN_CLIENT_ID") and cfg.get("LINKEDIN_CLIENT_SECRET")),
        "connected": bool(d.get("access_token")),
        "login_page": f"{base}/login?key=<LINKEDIN_LOGIN_KEY from ~/.config/linkedin/env>",
        "can_do": "read basic profile (name, photo, email) and publish/delete feed posts",
        "cannot_do": "edit profile sections (headline, about, experience, skills) — LinkedIn offers no API for that",
    }
    if d.get("access_token"):
        left = int((d.get("expires_at", 0) - time.time()) / 86400)
        out.update({"name": d.get("name"), "email": d.get("email"), "logged_in": d.get("logged_in"), "days_left": left, "expired": left < 0})
    return out


def whoami() -> dict[str, Any]:
    d = _auth()
    status_, _, me = _http("GET", "https://api.linkedin.com/v2/userinfo", token=d["access_token"])
    if status_ != 200:
        raise ValueError(f"userinfo failed ({status_}): {me}")
    return {k: me.get(k) for k in ("sub", "name", "given_name", "family_name", "email", "picture", "locale")}


def post(text: str, link_url: str = "", link_title: str = "", link_description: str = "", visibility: str = "PUBLIC") -> dict[str, Any]:
    d = _auth()
    if not text or not text.strip():
        raise ValueError("post text is empty")
    if len(text) > 3000:
        raise ValueError("LinkedIn posts are limited to 3000 characters")
    if visibility not in ("PUBLIC", "CONNECTIONS"):
        raise ValueError("visibility must be PUBLIC or CONNECTIONS")
    body: dict[str, Any] = {
        "author": f"urn:li:person:{d['sub']}",
        "commentary": text,
        "visibility": visibility,
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if link_url:
        art: dict[str, Any] = {"source": link_url}
        if link_title:
            art["title"] = link_title[:400]
        if link_description:
            art["description"] = link_description[:4086]
        body["content"] = {"article": art}
    status_, headers, resp = _http("POST", "https://api.linkedin.com/rest/posts", token=d["access_token"], body=body)
    if status_ not in (200, 201):
        raise ValueError(f"post failed ({status_}): {resp}")
    urn = headers.get("x-restli-id") or headers.get("X-RestLi-Id") or ""
    return {"status": "published", "post_urn": urn, "url": f"https://www.linkedin.com/feed/update/{urn}/" if urn else "", "visibility": visibility}


def delete_post(post_urn: str) -> dict[str, Any]:
    d = _auth()
    if not post_urn.startswith("urn:li:"):
        raise ValueError("post_urn must look like urn:li:share:123 or urn:li:ugcPost:123")
    status_, _, resp = _http("DELETE", "https://api.linkedin.com/rest/posts/" + urllib.parse.quote(post_urn, safe=""), token=d["access_token"])
    if status_ not in (200, 204):
        raise ValueError(f"delete failed ({status_}): {resp}")
    return {"status": "deleted", "post_urn": post_urn}
