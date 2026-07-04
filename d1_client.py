"""
d1_client.py — Sync layer between local ~/.qwoted state files and Cloudflare D1.

WHY THIS EXISTS
----------------
The original qwoted_*.py scripts read/write local files under qwoted_home()
(~/.qwoted by default). GitHub Actions runners are ephemeral — that folder
disappears after every job. Rather than rewriting qwoted_common.py /
qwoted_search.py / qwoted_pitch.py / qwoted_profile.py (risking breakage of
a tested, working skill), this script wraps them:

    1. `python3 d1_client.py pull`   → D1 rows written into ~/.qwoted/*
    2. ... run qwoted_search.py / qwoted_pitch.py / qwoted_profile.py as normal ...
    3. `python3 d1_client.py push`   → ~/.qwoted/* changes written back into D1

This keeps the original skill scripts completely untouched.

AUTH (set as GitHub Secrets / Cloudflare env vars)
---------------------------------------------------
CF_ACCOUNT_ID       Cloudflare account ID
CF_D1_DATABASE_ID   D1 database UUID (from `wrangler d1 info qwoted-agent-db`)
CF_API_TOKEN        Cloudflare API token with "D1 Edit" permission

Uses Cloudflare's D1 HTTP API directly:
https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Reuse the same state-directory logic as the rest of the skill so paths
# always match, without importing qwoted_common (keeps this file standalone
# and safe to run before qwoted_common's dependencies are even needed).
QWOTED_HOME = Path(os.environ.get("QWOTED_HOME") or (Path.home() / ".qwoted")).expanduser().resolve()
SESSION_FILE = QWOTED_HOME / "storage_state.json"
PITCHES_FILE = QWOTED_HOME / "sent_pitches.json"
PROFILE_FILE = QWOTED_HOME / "profile_state.json"
OPPORTUNITIES_DIR = QWOTED_HOME / "opportunities"

CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_D1_DATABASE_ID = os.environ.get("CF_D1_DATABASE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")

API_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
    f"/d1/database/{CF_D1_DATABASE_ID}/query"
    if CF_ACCOUNT_ID and CF_D1_DATABASE_ID
    else None
)


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] d1_client: {msg}", file=sys.stderr, flush=True)


def _require_config() -> None:
    missing = [
        name
        for name, val in [
            ("CF_ACCOUNT_ID", CF_ACCOUNT_ID),
            ("CF_D1_DATABASE_ID", CF_D1_DATABASE_ID),
            ("CF_API_TOKEN", CF_API_TOKEN),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Missing required env vars for D1 access: {', '.join(missing)}. "
            "Set these as GitHub Secrets and export them in the workflow."
        )


def d1_query(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    """Run one SQL statement against D1 via Cloudflare's REST API.
    Returns the list of result rows (empty list for writes with no RETURNING)."""
    _require_config()
    try:
        resp = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {CF_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"sql": sql, "params": params or []},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as e:
        raise RuntimeError(f"D1 request failed: {e}") from e

    if not body.get("success"):
        raise RuntimeError(f"D1 query failed: {body.get('errors')}")

    results = body.get("result") or []
    if not results:
        return []
    return results[0].get("results") or []


# ---------------------------------------------------------------------------
# PULL: D1 -> local files (run BEFORE the qwoted_*.py scripts)
# ---------------------------------------------------------------------------
def pull_session() -> bool:
    rows = d1_query("SELECT storage_state_json FROM session WHERE id = 1")
    if not rows:
        _log("WARNING: no session row in D1 yet — run local bootstrap first "
             "(see README_DEPLOY.md Phase 0).")
        return False
    QWOTED_HOME.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(rows[0]["storage_state_json"])
    _log(f"pulled session -> {SESSION_FILE}")
    return True


def pull_profile() -> None:
    rows = d1_query("SELECT profile_state_json FROM profile WHERE id = 1")
    if rows:
        PROFILE_FILE.write_text(rows[0]["profile_state_json"])
        _log(f"pulled profile -> {PROFILE_FILE}")
    else:
        _log("no profile row in D1 yet (fine on first run before `qwoted_profile.py create`)")


def pull_sent_pitches() -> None:
    rows = d1_query(
        "SELECT raw_json FROM pitches WHERE status = 'sent' ORDER BY id ASC"
    )
    entries = [json.loads(r["raw_json"]) for r in rows if r.get("raw_json")]
    PITCHES_FILE.write_text(json.dumps(entries, indent=2, default=str))
    _log(f"pulled {len(entries)} sent-pitch entries -> {PITCHES_FILE}")


def pull_all() -> None:
    """Full pull: session + profile + sent pitches. Call this at the start
    of every GitHub Actions job, before running any qwoted_*.py script."""
    ok = pull_session()
    if not ok:
        # Without a session, downstream scripts will fail fast with a clear
        # "run qwoted_login.py" error anyway — surface it early and stop.
        _log_event("session_expired", "No session found in D1 during pull.")
        raise SystemExit(
            "No Qwoted session in D1. Bootstrap it once locally "
            "(python3 qwoted_login.py) then upload via Phase 0 steps."
        )
    pull_profile()
    pull_sent_pitches()


# ---------------------------------------------------------------------------
# PUSH: local files -> D1 (run AFTER the qwoted_*.py scripts)
# ---------------------------------------------------------------------------
def push_opportunities() -> int:
    """Scan every *.json search-result file dropped by qwoted_search.py in
    OPPORTUNITIES_DIR and upsert each opportunity as its own D1 row."""
    if not OPPORTUNITIES_DIR.exists():
        return 0
    count = 0
    now = datetime.now(timezone.utc).isoformat()
    for fp in OPPORTUNITIES_DIR.glob("*.json"):
        try:
            payload = json.loads(fp.read_text())
        except Exception as e:
            _log(f"WARNING: could not parse {fp}: {e}")
            continue
        for opp in payload.get("opportunities", []):
            sr_id = opp.get("source_request_id")
            if not sr_id:
                continue
            d1_query(
                """
                INSERT INTO opportunities
                    (source_request_id, name, details, publication, deadline,
                     hashtags, url, raw_json, status, scraped_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                ON CONFLICT(source_request_id) DO UPDATE SET
                    name=excluded.name,
                    details=excluded.details,
                    raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
                """,
                [
                    sr_id,
                    opp.get("name", ""),
                    opp.get("details", ""),
                    (opp.get("publication") or {}).get("name", "")
                        if isinstance(opp.get("publication"), dict) else str(opp.get("publication", "")),
                    opp.get("deadline", ""),
                    ",".join(opp.get("hashtags") or []) if isinstance(opp.get("hashtags"), list) else "",
                    opp.get("url", ""),
                    json.dumps(opp, default=str),
                    payload.get("scraped_at", now),
                    now,
                ],
            )
            count += 1
        # Once synced to D1, remove the local copy so the next pull/push
        # cycle doesn't re-insert duplicates from a stale runner filesystem.
        fp.unlink(missing_ok=True)
    _log(f"pushed {count} opportunities -> D1")
    return count


def push_sent_pitches() -> int:
    if not PITCHES_FILE.exists():
        return 0
    try:
        entries = json.loads(PITCHES_FILE.read_text())
    except Exception as e:
        _log(f"WARNING: could not parse {PITCHES_FILE}: {e}")
        return 0

    # Only push entries that actually have a sent_at (i.e. real sends, not
    # partial/failed attempts) and aren't already recorded.
    existing = {
        r["source_request_id"]
        for r in d1_query("SELECT source_request_id FROM pitches WHERE status = 'sent'")
    }
    now = datetime.now(timezone.utc).isoformat()
    pushed = 0
    for e in entries:
        sr_id = e.get("source_request_id")
        if not sr_id or not e.get("sent_at") or sr_id in existing:
            continue
        d1_query(
            """
            INSERT INTO pitches
                (source_request_id, pitch_text, research_page_url, status, raw_json, created_at, sent_at)
            VALUES (?, ?, ?, 'sent', ?, ?, ?)
            """,
            [
                sr_id,
                e.get("pitch_text") or e.get("pitch") or "",
                e.get("research_page_url", ""),
                json.dumps(e, default=str),
                now,
                e.get("sent_at"),
            ],
        )
        d1_query(
            "UPDATE opportunities SET status = 'pitched', updated_at = ? WHERE source_request_id = ?",
            [now, sr_id],
        )
        pushed += 1
    _log(f"pushed {pushed} new sent-pitch records -> D1")
    return pushed


def push_profile() -> None:
    if not PROFILE_FILE.exists():
        return
    content = PROFILE_FILE.read_text()
    now = datetime.now(timezone.utc).isoformat()
    d1_query(
        """
        INSERT INTO profile (id, profile_state_json, updated_at) VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET profile_state_json = excluded.profile_state_json, updated_at = excluded.updated_at
        """,
        [content, now],
    )
    _log("pushed profile -> D1")


def push_all() -> None:
    push_opportunities()
    push_sent_pitches()
    push_profile()


def bootstrap_session() -> None:
    """One-time (and every ~30 days on re-login): push a freshly created
    local storage_state.json into D1. Run this LOCALLY right after
    `python3 qwoted_login.py`, never in GitHub Actions."""
    if not SESSION_FILE.exists():
        raise SystemExit(
            f"No {SESSION_FILE} found. Run `python3 qwoted_login.py` first."
        )
    content = SESSION_FILE.read_text()
    # sanity check it actually parses and has cookies before uploading
    parsed = json.loads(content)
    if not parsed.get("cookies"):
        raise SystemExit("storage_state.json has no cookies — login may have failed.")

    now = datetime.now(timezone.utc).isoformat()
    d1_query(
        """
        INSERT INTO session (id, storage_state_json, updated_at) VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET storage_state_json = excluded.storage_state_json, updated_at = excluded.updated_at
        """,
        [content, now],
    )
    _log_event("session_bootstrapped", f"cookies={len(parsed.get('cookies', []))}")
    _log(f"session uploaded to D1 ({len(parsed.get('cookies', []))} cookies)")


def log_event(event_type: str, detail: str = "") -> None:
    """Public helper other scripts (or the Worker) can call to record
    something in agent_events for the Telegram /status command."""
    _log_event(event_type, detail)


def _log_event(event_type: str, detail: str) -> None:
    try:
        d1_query(
            "INSERT INTO agent_events (event_type, detail, created_at) VALUES (?, ?, ?)",
            [event_type, detail, datetime.now(timezone.utc).isoformat()],
        )
    except Exception as e:
        # Never let event logging break the actual job.
        _log(f"WARNING: could not log event to D1: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("pull", "push", "bootstrap-session"):
        print("Usage: python3 d1_client.py [pull|push|bootstrap-session]", file=sys.stderr)
        sys.exit(1)

    try:
        if sys.argv[1] == "pull":
            pull_all()
        elif sys.argv[1] == "push":
            push_all()
        else:
            bootstrap_session()
    except Exception as e:
        _log(f"FATAL: {e}")
        _log_event("error", str(e))
        sys.exit(1)
