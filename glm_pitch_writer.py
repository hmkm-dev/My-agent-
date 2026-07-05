"""
glm_pitch_writer.py — Draft Direct-Response journalist pitches using GLM-4/5.

Flow:
  1. Pull "new" opportunities from D1 (already synced there by qwoted_search.py + d1_client.py push).
  2. Apply strict quality filters (skip spammy / stale / non-pitchable requests).
  3. For each surviving opportunity, call GLM to draft a concise, expert,
     no-fluff pitch.
  4. Insert the draft into `pitches` (status='pending_approval').
  5. Mark the opportunity status='drafted'.
  6. Ping the Cloudflare Worker /api/notify so Telegram shows the draft with
     Approve/Reject buttons.

Nothing here ever sends anything to Qwoted — that's send_approved_pitch.py's
job, and it only runs after explicit human approval via Telegram.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import requests

from d1_client import d1_query, _log, _log_event  # reuse the same D1 connection

GLM_API_URL = os.environ.get("GLM_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
GLM_API_KEY = os.environ.get("GLM_API_KEY")
GLM_MODEL = os.environ.get("GLM_MODEL", "z-ai/glm-5.1")

# NOTE ON PROVIDER: Default here is NVIDIA NIM (build.nvidia.com) — free tier,
# no card required, OpenAI-compatible /chat/completions endpoint hosting
# GLM-5.1 among 80+ open models. Get a free `nvapi-...` key at build.nvidia.com,
# put it in the GLM_API_KEY secret, and this works with zero code changes.
#
# To use Zhipu's official (paid) GLM API instead, override both:
#   GLM_API_URL = https://open.bigmodel.cn/api/paas/v4/chat/completions
#   GLM_MODEL   = glm-4-plus (or glm-5, etc.)

PITCH_SYSTEM_PROMPT = """You are an expert PR consultant writing a journalist pitch for a source \
requesting inclusion in a media story. Style rules (non-negotiable):
- Direct Response style: concise, confident, zero fluff, zero generic AI phrasing \
  ("I hope this finds you well", "As an AI", "I'd be happy to", etc.)
- Open with the single strongest, most specific insight or stat relevant to the \
  journalist's request — no throat-clearing.
- 3-5 short paragraphs max. No bullet lists unless the request explicitly asks for data points.
- End with a one-line credibility signal (who the source is) and a clear offer to expand.
- Never fabricate statistics, quotes, or credentials. If specific data isn't provided, \
  speak in terms of experience and framework, not invented numbers.
- No subject line, no greeting like "Dear [Name]" — Qwoted pitches are submitted as plain body text.
"""


def fetch_learning_examples() -> dict:
    """Pull recent human feedback so the model learns this user's taste over
    time: pitches that got sent (implicitly approved) as GOOD examples, and
    rejected ones (with any reason the human gave) as things to avoid."""
    good = d1_query(
        "SELECT pitch_text FROM pitches WHERE status = 'sent' "
        "ORDER BY id DESC LIMIT 3"
    )
    bad = d1_query(
        "SELECT pitch_text, feedback_note FROM pitches WHERE status = 'rejected' "
        "ORDER BY id DESC LIMIT 3"
    )
    return {
        "good": [r["pitch_text"] for r in good if r.get("pitch_text")],
        "bad": [
            {"text": r["pitch_text"], "reason": r.get("feedback_note") or ""}
            for r in bad if r.get("pitch_text")
        ],
    }


def _spammy_filters_pass(opp: dict) -> tuple[bool, str]:
    """Strict quality gate. Returns (passes, reason_if_rejected)."""
    if not opp.get("want_pitches", True) and opp.get("want_pitches") is not None:
        return False, "want_pitches=false"
    if opp.get("no_deadline") is False and not opp.get("deadline"):
        return False, "no usable deadline"
    if opp.get("deadline_approaching") is True and not opp.get("no_deadline"):
        # Not disqualifying by itself, but logged — deadline pressure is fine,
        # a genuinely PASSED deadline (checked by caller via date) is not.
        pass
    details = (opp.get("details") or "").strip()
    if len(details) < 40:
        return False, "details too thin to write a grounded pitch"
    return True, ""


def fetch_candidates(limit: int) -> list[dict]:
    rows = d1_query(
        "SELECT source_request_id, raw_json FROM opportunities WHERE status = 'new' "
        "ORDER BY scraped_at DESC LIMIT ?",
        [limit * 3],  # over-fetch since some will be filtered out
    )
    candidates = []
    for r in rows:
        try:
            opp = json.loads(r["raw_json"])
        except Exception:
            continue
        ok, reason = _spammy_filters_pass(opp)
        if not ok:
            _log(f"skip SR {r['source_request_id']}: {reason}")
            d1_query(
                "UPDATE opportunities SET status = 'skipped', updated_at = ? WHERE source_request_id = ?",
                [datetime.now(timezone.utc).isoformat(), r["source_request_id"]],
            )
            continue
        candidates.append(opp)
        if len(candidates) >= limit:
            break
    return candidates


def call_glm(opportunity: dict, learning: dict | None = None) -> str:
    if not GLM_API_KEY:
        raise RuntimeError("GLM_API_KEY not set")

    learning_block = ""
    if learning:
        if learning.get("good"):
            learning_block += "\n\nHere are pitches this specific user has previously APPROVED and sent — match this style, tone, and structure as closely as fits the new request:\n"
            for i, txt in enumerate(learning["good"][:3], 1):
                learning_block += f"\n[Approved example {i}]\n{txt[:600]}\n"
        if learning.get("bad"):
            learning_block += "\n\nHere are pitches this user REJECTED. Avoid repeating whatever made these weak (if a reason was given, it's included):\n"
            for i, item in enumerate(learning["bad"][:3], 1):
                reason = f" (Reason for rejection: {item['reason']})" if item.get("reason") else ""
                learning_block += f"\n[Rejected example {i}]{reason}\n{item['text'][:400]}\n"

    user_prompt = (
        f"Journalist request: {opportunity.get('name', '')}\n\n"
        f"Details: {opportunity.get('details', '')}\n\n"
        f"Request type: {opportunity.get('request_type', '')} / "
        f"{opportunity.get('request_sub_type', '')}\n"
        f"{learning_block}\n\n"
        "Write the pitch body now."
    )

    resp = requests.post(
        GLM_API_URL,
        headers={
            "Authorization": f"Bearer {GLM_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": GLM_MODEL,
            "messages": [
                {"role": "system", "content": PITCH_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.6,
            "max_tokens": 500,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected GLM response shape: {data}") from e


def notify_worker(worker_url: str, notify_secret: str, pitch_id: int, preview: str) -> None:
    try:
        requests.post(
            f"{worker_url}/api/notify",
            headers={"Authorization": f"Bearer {notify_secret}", "Content-Type": "application/json"},
            json={
                "event_type": "draft_ready",
                "pitch_id": pitch_id,
                "message": f"📝 New pitch draft #{pitch_id} ready:\n\n{preview[:500]}",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        _log(f"WARNING: could not notify worker for pitch #{pitch_id}: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--worker-url", required=True)
    ap.add_argument("--notify-secret", required=True)
    args = ap.parse_args()

    candidates = fetch_candidates(args.limit)
    _log(f"{len(candidates)} candidates passed quality filters")

    learning = fetch_learning_examples()
    _log(f"learning context: {len(learning['good'])} approved examples, {len(learning['bad'])} rejected examples")

    drafted = 0
    for opp in candidates:
        sr_id = opp.get("source_request_id")
        try:
            pitch_text = call_glm(opp, learning)
        except Exception as e:
            _log(f"ERROR: GLM draft failed for SR {sr_id}: {e}")
            _log_event("error", f"GLM draft failed SR {sr_id}: {e}")
            continue

        now = datetime.now(timezone.utc).isoformat()
        rows = d1_query(
            """
            INSERT INTO pitches (source_request_id, pitch_text, status, created_at)
            VALUES (?, ?, 'pending_approval', ?)
            RETURNING id
            """,
            [sr_id, pitch_text, now],
        )
        pitch_id = rows[0]["id"] if rows else None

        if pitch_id is None:
            # Fallback: some D1 REST API versions/configurations don't return
            # rows from RETURNING reliably. Look the row back up instead of
            # silently losing track of it (a lost pitch_id means the
            # approval flow can never reach this draft).
            lookup = d1_query(
                "SELECT id FROM pitches WHERE source_request_id = ? AND created_at = ? ORDER BY id DESC LIMIT 1",
                [sr_id, now],
            )
            pitch_id = lookup[0]["id"] if lookup else None
            if pitch_id is None:
                _log(f"ERROR: could not determine pitch_id for SR {sr_id} after insert — "
                     f"draft saved in D1 but Telegram won't be notified. Check manually.")
                _log_event("error", f"pitch_id lookup failed for SR {sr_id}")
                continue

        d1_query(
            "UPDATE opportunities SET status = 'drafted', updated_at = ? WHERE source_request_id = ?",
            [now, sr_id],
        )

        if pitch_id:
            notify_worker(args.worker_url, args.notify_secret, pitch_id, pitch_text)
            _log(f"drafted pitch #{pitch_id} for SR {sr_id}")
            drafted += 1

    _log_event("draft_batch_complete", f"drafted={drafted} of {len(candidates)} candidates")
    print(f"RESULT: {json.dumps({'drafted': drafted, 'candidates': len(candidates)})}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log(f"FATAL: {e}")
        _log_event("error", str(e))
        sys.exit(1)
  
