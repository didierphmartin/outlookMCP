"""Outlook MCP server settings, from the environment (run.sh loads .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from cryptography.fernet import Fernet


@dataclass(frozen=True)
class AppConfig:
    client_id: str                      # Entra ID app registration (any org + personal accounts)
    secret_key: str                     # Fernet key: encrypts refresh tokens at rest
    db_path: str
    client_secret: str = ""             # redirect fallback with a confidential app only
    public_url: str = ""                # https://... or http://localhost...: enables the redirect fallback
    authority: str = "https://login.microsoftonline.com/common"
    graph_base: str = "https://graph.microsoft.com/v1.0"
    host: str = "127.0.0.1"
    port: int = 8770
    attachment_max_bytes: int = 10_000_000
    fake_graph: bool = False
    fake_seed: str = ""                 # JSON file of messages for fake mode

    SCOPES: ClassVar[str] = "openid profile offline_access User.Read Mail.Read"

    @staticmethod
    def new_secret_key() -> str:
        return Fernet.generate_key().decode()

    @classmethod
    def from_env(cls) -> "AppConfig":
        env = os.environ
        fake = env.get("OUTLOOK_FAKE_GRAPH", "") == "1"
        if not fake:
            missing = [n for n in ("OUTLOOK_CLIENT_ID", "OUTLOOK_SECRET_KEY") if not env.get(n)]
            if missing:
                raise SystemExit(f"missing in .env: {', '.join(missing)} (see README)")
        return cls(
            client_id=env.get("OUTLOOK_CLIENT_ID", "fake-client"),
            secret_key=env.get("OUTLOOK_SECRET_KEY") or cls.new_secret_key(),
            db_path=env.get("OUTLOOK_DB_PATH") or str(Path.home() / "Documents" / "synergyAI" / "outlook_mcp.sqlite"),
            client_secret=env.get("OUTLOOK_CLIENT_SECRET", ""),
            public_url=env.get("OUTLOOK_PUBLIC_URL", "").rstrip("/"),
            host=env.get("OUTLOOK_HOST", "127.0.0.1"),
            port=int(env.get("OUTLOOK_PORT", "8770")),
            attachment_max_bytes=int(env.get("OUTLOOK_ATTACHMENT_MAX_BYTES", "10000000")),
            fake_graph=fake,
            fake_seed=env.get("OUTLOOK_FAKE_SEED", ""),
        )
