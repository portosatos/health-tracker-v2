"""ORM-модели. Зеркалят схему из docs/DESIGN.md, раздел 3 (миграция — migrations/versions/0001_initial.py).

Расширения относительно эскиза: uploads.cancel_requested, outbox.locked_at, таблица worker_heartbeats.
Первая запись tz_periods хранит valid_from = '-infinity' (в Python — db.NEG_INF).
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, ForeignKey, Index, Integer, LargeBinary, SmallInteger,
    Text, Time, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TS = TIMESTAMP(timezone=True)


def user_fk() -> ForeignKey:
    return ForeignKey("users.id", ondelete="CASCADE")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    login: Mapped[str] = mapped_column(CITEXT, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))
    expires_at: Mapped[datetime] = mapped_column(TS)


class UserSettings(Base):
    __tablename__ = "user_settings"
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk(), primary_key=True)
    email: Mapped[str | None] = mapped_column(Text)
    email_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    quiet_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    quiet_start: Mapped[time] = mapped_column(Time, server_default=text("'22:00'"))
    quiet_end: Mapped[time] = mapped_column(Time, server_default=text("'08:00'"))
    follow_device_tz: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    tz_version: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class TzPeriod(Base):
    __tablename__ = "tz_periods"
    __table_args__ = (
        CheckConstraint("(tz_name IS NULL) <> (fixed_offset_min IS NULL)"),
        UniqueConstraint("user_id", "valid_from"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    valid_from: Mapped[datetime] = mapped_column(TS)
    tz_name: Mapped[str | None] = mapped_column(Text)
    fixed_offset_min: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("user_id", "external_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    external_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, server_default=text("'other'"))
    model: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, server_default=text("5"))
    priority_manual: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    filename: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    byte_offset: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    lines_total: Mapped[int | None] = mapped_column(BigInteger)
    lines_done: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    rows_inserted: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    rows_updated: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    rows_duplicate: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    rows_rejected: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    min_ts: Mapped[datetime | None] = mapped_column(TS)
    max_ts: Mapped[datetime | None] = mapped_column(TS)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class UploadError(Base):
    __tablename__ = "upload_errors"
    upload_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("uploads.id", ondelete="CASCADE"), primary_key=True)
    line_no: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)


class SleepInterval(Base):
    __tablename__ = "sleep_intervals"
    __table_args__ = (Index("ix_sleep_user_end", "user_id", "end_ts"),)
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    device_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    start_ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    end_ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    stage: Mapped[str] = mapped_column(Text)
    offset_min: Mapped[int | None] = mapped_column(Integer)
    upload_id: Mapped[int | None] = mapped_column(BigInteger)


class StepsMinute(Base):
    __tablename__ = "steps_minute"
    __table_args__ = (CheckConstraint("steps >= 0"), Index("ix_steps_user_minute", "user_id", "minute_ts"))
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    device_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    minute_ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    steps: Mapped[int] = mapped_column(Integer)
    offset_min: Mapped[int | None] = mapped_column(Integer)
    upload_id: Mapped[int | None] = mapped_column(BigInteger)


class HrPoint(Base):
    __tablename__ = "hr_points"
    __table_args__ = (Index("ix_hr_user_ts", "user_id", "ts"),)
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    device_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    bpm: Mapped[int] = mapped_column(SmallInteger)
    offset_min: Mapped[int | None] = mapped_column(Integer)
    upload_id: Mapped[int | None] = mapped_column(BigInteger)


class DailySummary(Base):
    __tablename__ = "daily_summary"
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk(), primary_key=True)
    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    day_start: Mapped[datetime] = mapped_column(TS)
    day_end: Mapped[datetime] = mapped_column(TS)
    day_length_min: Mapped[int] = mapped_column(Integer)
    wear_minutes: Mapped[int] = mapped_column(Integer)
    coverage: Mapped[str] = mapped_column(Text)
    steps: Mapped[int | None] = mapped_column(Integer)
    steps_status: Mapped[str] = mapped_column(Text)
    active_minutes: Mapped[int | None] = mapped_column(Integer)
    sleep_minutes: Mapped[int | None] = mapped_column(Integer)
    sleep_start: Mapped[datetime | None] = mapped_column(TS)
    sleep_end: Mapped[datetime | None] = mapped_column(TS)
    sleep_status: Mapped[str] = mapped_column(Text)
    resting_hr: Mapped[int | None] = mapped_column(Integer)
    peak_hr: Mapped[int | None] = mapped_column(Integer)
    avg_hr: Mapped[int | None] = mapped_column(Integer)
    hr_minutes: Mapped[int | None] = mapped_column(Integer)
    hr_status: Mapped[str] = mapped_column(Text)
    tz_version: Mapped[int] = mapped_column(Integer)
    computed_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))


class Goal(Base):
    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint("kind IN ('steps_per_day','sleep_minutes_per_day')"),
        CheckConstraint("target > 0"),
        UniqueConstraint("user_id", "kind"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    kind: Mapped[str] = mapped_column(Text)
    target: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class GoalAchievement(Base):
    __tablename__ = "goal_achievements"
    __table_args__ = (UniqueConstraint("goal_id", "local_date"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    goal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("goals.id", ondelete="CASCADE"))
    local_date: Mapped[date] = mapped_column(Date)
    target_at_time: Mapped[int] = mapped_column(Integer)
    value: Mapped[int] = mapped_column(Integer)
    achieved_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))
    revoked_at: Mapped[datetime | None] = mapped_column(TS)


class RuleSetting(Base):
    __tablename__ = "rule_settings"
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk(), primary_key=True)
    rule_id: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    changed_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (
        CheckConstraint("status IN ('open','resolved')"),
        CheckConstraint("feedback IN ('helpful','not_helpful')"),
        Index("uq_reco_open", "user_id", "rule_id", "dedup_key", unique=True, postgresql_where=text("status = 'open'")),
        Index("ix_reco_user_status", "user_id", "status", text("opened_at DESC")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    rule_id: Mapped[str] = mapped_column(Text)
    dedup_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    resolution_reason: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    evidence: Mapped[Any] = mapped_column(JSONB)
    opened_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    feedback: Mapped[str | None] = mapped_column(Text)
    feedback_at: Mapped[datetime | None] = mapped_column(TS)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("user_id", "dedup_key"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[Any] = mapped_column(JSONB)
    dedup_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))
    read_at: Mapped[datetime | None] = mapped_column(TS)


class DigestPending(Base):
    __tablename__ = "digest_pending"
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk(), primary_key=True)
    recommendation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recommendations.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        Index("ix_outbox_status_send_after", "status", "send_after"),
        Index("uq_outbox_open", "user_id", unique=True, postgresql_where=text("status IN ('pending','failed')")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, user_fk())
    to_addr: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    recommendation_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    send_after: Mapped[datetime] = mapped_column(TS)
    defer_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))
    sent_at: Mapped[datetime | None] = mapped_column(TS)
    locked_at: Mapped[datetime | None] = mapped_column(TS)  # для возврата протухшего 'sending' (раздел 10)


class MailInbox(Base):
    __tablename__ = "mail_inbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[str] = mapped_column(Text, unique=True)
    rcpt: Mapped[str] = mapped_column(Text)
    raw: Mapped[str] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("uq_jobs_dedupe", "dedupe_key", unique=True,
              postgresql_where=text("status = 'queued' AND dedupe_key IS NOT NULL")),
        Index("ix_jobs_status_run_at", "status", "run_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[Any] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    run_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("5"))
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(TS)
    heartbeat_at: Mapped[datetime | None] = mapped_column(TS)
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_user_id", "user_id", "id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=text("now()"))


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(TS)
