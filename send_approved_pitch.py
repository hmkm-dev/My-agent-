"""
send_approved_pitch.py — The ONLY script in this system that actually POSTs
a pitch to Qwoted (a real journalist). Runs exclusively from send-pitch.yml,
which only fires after a human taps "Approve" in Telegram.

Wraps the original, untouched qwoted_pitch.py CLI via subprocess rather than
importing its internals — keeps this integration layer decoupled from the
skill's implementation details.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from d1_client import d1_query, _log, _log_event


def get_approved_pitch(pitch_id: int) -> dict:
    rows = d1_query(
        "SELECT id, source_request_id, pitch_text, research_page_url, status "
        "FROM pitches WHERE id = ?",
        [pitch_id],
    )
    if not rows:
        raise RuntimeError(f"Pitch #{pitch_id} not found in D1")
    row = rows[0]
    if row["status"] != "approved":
        raise RuntimeError(
            f"Pitch #{pitch_id} has status '{row['status']}', expected 'approved'. "
            "Refusing to send — this should never happen unless dispatched manually."
        )
    return row


def send_pitch(row: dict) -> None:
    sr_id = row["source_request_id"]
    pitch_text = row["pitch_text"] or ""
    if not pitch_text.strip():
        raise RuntimeError(f"Pitch #{row['id']} has empty pitch_text — refusing to send.")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(pitch_text)
        pitch_file = f.name

    cmd = [
        sys.executable, "qwoted_pitch.py",
        "--source-request-id", str(sr_id),
        "--pitch-text-file", pitch_file,
        "--send",
    ]
    if row.get("research_page_url"):
        cmd += ["--research-page-url", row["research_page_url"]]

    _log(f"running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    Path(pitch_file).unlink(missing_ok=True)

    stdout = result.stdout or ""
    _log(f"qwoted_pitch.py stdout:\n{stdout}")
    if result.stderr:
        _log(f"qwoted_pitch.py stderr:\n{result.stderr}")

    result_line = next((l for l in stdout.splitlines() if l.startswith("RESULT:")), None)
    ok = result.returncode == 0
    status_value = None
    if result_line:
        try:
            payload = json.loads(result_line[len("RESULT:"):].strip())
            status_value = payload.get("status")
            # qwoted_pitch.py's contract (see result_line() calls in qwoted_pitch.py):
            #   "sent"              -> real send succeeded
            #   "draft_only"        -> --send wasn't passed (shouldn't happen here, but not success)
            #   "skipped_duplicate" -> already pitched before (treat as failure so a human checks)
            #   "error"             -> explicit failure
            ok = ok and status_value == "sent"
        except json.JSONDecodeError:
            ok = False
    else:
        # No RESULT line at all is unexpected regardless of exit code — treat as failure.
        ok = False

    if not ok:
        raise RuntimeError(
            f"qwoted_pitch.py did not confirm a successful send for SR {sr_id} "
            f"(exit code {result.returncode}, status='{status_value}'). See logs above."
        )


def mark_sent(pitch_id: int, sr_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    d1_query(
        "UPDATE pitches SET status = 'sent', sent_at = ? WHERE id = ?",
        [now, pitch_id],
    )
    d1_query(
        "UPDATE opportunities SET status = 'pitched', updated_at = ? WHERE source_request_id = ?",
        [now, sr_id],
    )
    _log_event("pitch_sent", f"pitch_id={pitch_id} source_request_id={sr_id}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pitch-id", type=int, required=True)
    args = ap.parse_args()

    row = get_approved_pitch(args.pitch_id)
    send_pitch(row)
    mark_sent(args.pitch_id, row["source_request_id"])
    _log(f"pitch #{args.pitch_id} sent successfully for SR {row['source_request_id']}")
    print(f"RESULT: {json.dumps({'ok': True, 'pitch_id': args.pitch_id})}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log(f"FATAL: {e}")
        _log_event("error", str(e))
        print(f"RESULT: {json.dumps({'ok': False, 'error': str(e)})}")
        sys.exit(1)
