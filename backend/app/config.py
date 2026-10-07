"""Конфигурация из переменных окружения / backend/.env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BACKEND_DIR / ".env"), extra="ignore")

    database_url: str = "postgresql+psycopg://ht2:ht2_dev_password@localhost:5432/healthtracker_v2"
    test_database_url: str = "postgresql+psycopg://ht2:ht2_dev_password@localhost:5432/healthtracker_v2_test"
    ht_env: str = "dev"  # dev | test | prod
    api_port: int = 8000
    smtp_sink_port: int = 2525
    upload_dir: str = "data/uploads"
    chunk_lines: int = 2000
    digest_hold_seconds: int = 60
    worker_concurrency: int = 2
    default_timezone: str = "Europe/Moscow"

    # Пороги покрытия (DESIGN, раздел 6, «Спорные места» п.9)
    wear_partial_min: int = 120
    wear_complete_min: int = 720
    sleep_min_minutes: int = 60
    hr_min_minutes: int = 30
    resting_min_candidates: int = 30
    session_gap_min: int = 30

    max_upload_bytes: int = 500 * 1024 * 1024
    max_line_bytes: int = 4096
    session_days: int = 14

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    @property
    def psycopg_url(self) -> str:
        return self.sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)

    @property
    def upload_path(self) -> Path:
        p = Path(self.upload_dir)
        return p if p.is_absolute() else REPO_DIR / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
