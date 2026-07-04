-- ============================================================================
-- Qwoted SEO Backlink Agent — Cloudflare D1 Schema
-- ============================================================================
-- Deploy: wrangler d1 execute qwoted-agent-db --file=./schema.sql --remote

-- Qwoted login session (Playwright storage_state.json), refreshed manually
-- ~every 30 days. Stored as raw JSON text (cookies array).
CREATE TABLE IF NOT EXISTS session (
  id INTEGER PRIMARY KEY CHECK (id = 1),   -- singleton row
  storage_state_json TEXT NOT NULL,        -- full storage_state.json content
  updated_at TEXT NOT NULL,
  expires_estimate TEXT                    -- updated_at + ~30 days, informational
);

-- User's "Source" persona (from qwoted_profile.py) — mirrors profile_state.json
CREATE TABLE IF NOT EXISTS profile (
  id INTEGER PRIMARY KEY CHECK (id = 1),   -- singleton row
  profile_state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Scraped journalist opportunities (mirrors ~/.qwoted/opportunities/*.json)
CREATE TABLE IF NOT EXISTS opportunities (
  source_request_id INTEGER PRIMARY KEY,
  name TEXT,
  details TEXT,
  publication TEXT,
  deadline TEXT,
  hashtags TEXT,
  url TEXT,
  raw_json TEXT NOT NULL,                  -- full scraped object, untouched
  quality_score INTEGER,                   -- filled by classify step
  status TEXT NOT NULL DEFAULT 'new',      -- new | scored | drafted | pitched | skipped | expired
  scraped_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opportunities_status ON opportunities(status);

-- Sent / drafted pitches (mirrors sent_pitches.json + adds GLM draft + approval flow)
CREATE TABLE IF NOT EXISTS pitches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_request_id INTEGER NOT NULL,
  pitch_text TEXT,
  research_page_url TEXT,
  status TEXT NOT NULL DEFAULT 'draft',    -- draft | pending_approval | approved | sent | rejected
  raw_json TEXT,                           -- full original entry (from sent_pitches.json or GLM draft)
  telegram_chat_id TEXT,
  telegram_message_id TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT,
  FOREIGN KEY (source_request_id) REFERENCES opportunities(source_request_id)
);

CREATE INDEX IF NOT EXISTS idx_pitches_status ON pitches(status);
CREATE INDEX IF NOT EXISTS idx_pitches_source_request ON pitches(source_request_id);

-- Simple audit/event log — every worker/action event lands here for /status command
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,                -- search_run | draft_created | pitch_sent | session_expired | error
  detail TEXT,
  created_at TEXT NOT NULL
);
