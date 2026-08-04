from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    vibe_api_base: str
    vibe_api_token: str
    live_control_key: str
    cors_origins: tuple[str, ...]
    request_timeout_seconds: float
    retry_attempts: int
    database_url: str = "sqlite:////tmp/vibepilot.db"
    public_base_url: str = ""
    vibe_webhook_secret: str = ""
    receipt_signing_key: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        configured_origins = tuple(
            item.strip()
            for item in os.getenv("CORS_ORIGINS", "*").split(",")
            if item.strip()
        )
        return cls(
            vibe_api_base=os.getenv(
                "VIBE_API_BASE", "https://lk.vibemarketolog.ru/api/agent"
            ).rstrip("/"),
            vibe_api_token=os.getenv("VIBE_API_TOKEN", "").strip(),
            live_control_key=os.getenv("VIBEPILOT_LIVE_KEY", "").strip(),
            cors_origins=configured_origins or ("*",),
            request_timeout_seconds=float(
                os.getenv("VIBE_REQUEST_TIMEOUT_SECONDS", "120")
            ),
            retry_attempts=max(1, int(os.getenv("VIBE_RETRY_ATTEMPTS", "3"))),
            database_url=os.getenv(
                "DATABASE_URL", "sqlite:////tmp/vibepilot.db"
            ).strip(),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            vibe_webhook_secret=os.getenv("VIBE_WEBHOOK_SECRET", "").strip(),
            receipt_signing_key=os.getenv("RECEIPT_SIGNING_KEY", "").strip(),
        )
