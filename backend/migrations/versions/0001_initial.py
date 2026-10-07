"""Полная схема БД (docs/DESIGN.md, раздел 3).

Revision ID: 0001
Revises:
"""
import re

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DDL = """
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE users (
  id bigserial PRIMARY KEY,
  login citext UNIQUE NOT NULL,
  password_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE auth_sessions (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  token_hash bytea UNIQUE NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);
CREATE INDEX ix_auth_sessions_user ON auth_sessions (user_id);
CREATE TABLE user_settings (
  user_id bigint PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  email text, email_enabled boolean NOT NULL DEFAULT true,
  quiet_enabled boolean NOT NULL DEFAULT false,
  quiet_start time NOT NULL DEFAULT '22:00', quiet_end time NOT NULL DEFAULT '08:00',
  follow_device_tz boolean NOT NULL DEFAULT true,
  tz_version int NOT NULL DEFAULT 0
);
CREATE TABLE tz_periods (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  valid_from timestamptz NOT NULL,
  tz_name text,
  fixed_offset_min int,
  source text NOT NULL,
  CHECK ((tz_name IS NULL) <> (fixed_offset_min IS NULL)),
  UNIQUE (user_id, valid_from)
);
CREATE TABLE devices (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  external_id text NOT NULL, kind text NOT NULL DEFAULT 'other', model text, name text,
  priority int NOT NULL DEFAULT 5, priority_manual boolean NOT NULL DEFAULT false,
  UNIQUE (user_id, external_id)
);

CREATE TABLE uploads (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  filename text, size_bytes bigint, path text NOT NULL,
  status text NOT NULL DEFAULT 'queued',
  byte_offset bigint NOT NULL DEFAULT 0,
  lines_total bigint, lines_done bigint NOT NULL DEFAULT 0,
  rows_inserted bigint NOT NULL DEFAULT 0, rows_updated bigint NOT NULL DEFAULT 0,
  rows_duplicate bigint NOT NULL DEFAULT 0, rows_rejected bigint NOT NULL DEFAULT 0,
  min_ts timestamptz, max_ts timestamptz, error text,
  cancel_requested boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz
);
CREATE INDEX ix_uploads_user ON uploads (user_id, id DESC);
CREATE TABLE upload_errors (
  upload_id bigint REFERENCES uploads ON DELETE CASCADE, line_no bigint, code text, message text,
  PRIMARY KEY (upload_id, line_no)
);

CREATE TABLE sleep_intervals (
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  device_id bigint NOT NULL REFERENCES devices ON DELETE CASCADE,
  start_ts timestamptz NOT NULL, end_ts timestamptz NOT NULL,
  stage text NOT NULL, offset_min int, upload_id bigint,
  PRIMARY KEY (device_id, start_ts, end_ts)
);
CREATE INDEX ix_sleep_user_end ON sleep_intervals (user_id, end_ts);

CREATE TABLE steps_minute (
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  device_id bigint NOT NULL REFERENCES devices ON DELETE CASCADE,
  minute_ts timestamptz NOT NULL, steps int NOT NULL CHECK (steps >= 0),
  offset_min int, upload_id bigint,
  PRIMARY KEY (device_id, minute_ts)
);
CREATE INDEX ix_steps_user_minute ON steps_minute (user_id, minute_ts);

CREATE TABLE hr_points (
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  device_id bigint NOT NULL REFERENCES devices ON DELETE CASCADE,
  ts timestamptz NOT NULL, bpm smallint NOT NULL, offset_min int, upload_id bigint,
  PRIMARY KEY (device_id, ts)
);
CREATE INDEX ix_hr_user_ts ON hr_points (user_id, ts);

CREATE TABLE daily_summary (
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE, local_date date NOT NULL,
  day_start timestamptz NOT NULL, day_end timestamptz NOT NULL, day_length_min int NOT NULL,
  wear_minutes int NOT NULL, coverage text NOT NULL,
  steps int, steps_status text NOT NULL,
  active_minutes int,
  sleep_minutes int, sleep_start timestamptz, sleep_end timestamptz, sleep_status text NOT NULL,
  resting_hr int, peak_hr int, avg_hr int, hr_minutes int, hr_status text NOT NULL,
  tz_version int NOT NULL, computed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, local_date)
);

CREATE TABLE goals (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('steps_per_day','sleep_minutes_per_day')),
  target int NOT NULL CHECK (target > 0), active boolean NOT NULL DEFAULT true,
  UNIQUE (user_id, kind)
);
CREATE TABLE goal_achievements (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  goal_id bigint NOT NULL REFERENCES goals ON DELETE CASCADE,
  local_date date NOT NULL, target_at_time int NOT NULL, value int NOT NULL,
  achieved_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz,
  UNIQUE (goal_id, local_date)
);

CREATE TABLE rule_settings (
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE, rule_id text NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  changed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (user_id, rule_id)
);
CREATE TABLE recommendations (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  rule_id text NOT NULL, dedup_key text NOT NULL,
  status text NOT NULL CHECK (status IN ('open','resolved')),
  resolution_reason text,
  title text NOT NULL, body text NOT NULL, evidence jsonb NOT NULL,
  opened_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), resolved_at timestamptz,
  feedback text CHECK (feedback IN ('helpful','not_helpful')), feedback_at timestamptz
);
CREATE UNIQUE INDEX uq_reco_open ON recommendations (user_id, rule_id, dedup_key) WHERE status = 'open';
CREATE INDEX ix_reco_user_status ON recommendations (user_id, status, opened_at DESC);

CREATE TABLE notifications (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  kind text NOT NULL,
  payload jsonb NOT NULL, dedup_key text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  read_at timestamptz, UNIQUE (user_id, dedup_key)
);
CREATE INDEX ix_notifications_user ON notifications (user_id, id DESC);
CREATE TABLE digest_pending (
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  recommendation_id bigint NOT NULL REFERENCES recommendations ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (user_id, recommendation_id)
);
CREATE TABLE outbox (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  to_addr text NOT NULL,
  subject text NOT NULL, body_text text NOT NULL, body_html text,
  recommendation_ids bigint[] NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  send_after timestamptz NOT NULL, defer_reason text,
  attempts int NOT NULL DEFAULT 0, last_error text, next_attempt_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz,
  locked_at timestamptz
);
CREATE INDEX ix_outbox_status_send_after ON outbox (status, send_after);
CREATE UNIQUE INDEX uq_outbox_open ON outbox (user_id) WHERE status IN ('pending','failed');
CREATE TABLE mail_inbox (
  id bigserial PRIMARY KEY, message_id text UNIQUE NOT NULL, rcpt text NOT NULL,
  raw text NOT NULL, subject text, received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
  id bigserial PRIMARY KEY, kind text NOT NULL,
  user_id bigint, payload jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'queued',
  run_at timestamptz NOT NULL DEFAULT now(), attempts int NOT NULL DEFAULT 0, max_attempts int NOT NULL DEFAULT 5,
  locked_by text, locked_at timestamptz, heartbeat_at timestamptz, last_error text,
  dedupe_key text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_jobs_dedupe ON jobs (dedupe_key) WHERE status = 'queued' AND dedupe_key IS NOT NULL;
CREATE INDEX ix_jobs_status_run_at ON jobs (status, run_at);
CREATE TABLE events (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL, type text NOT NULL, payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_events_user_id ON events (user_id, id);

CREATE TABLE worker_heartbeats (
  worker_id text PRIMARY KEY, beat_at timestamptz NOT NULL
);
"""


def upgrade() -> None:
    for stmt in [s for s in DDL.split(";\n") if s.strip()]:
        op.execute(stmt)


def downgrade() -> None:
    for t in re.findall(r"CREATE TABLE (\w+)", DDL):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
