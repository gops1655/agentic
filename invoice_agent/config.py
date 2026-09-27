"""Settings loaded from the environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _list(value: str | None) -> list[str]:
    return [v.strip().lower() for v in (value or "").split(",") if v.strip()]


@dataclass
class Settings:
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5"

    email_address: str = ""
    email_password: str = ""
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_folder: str = "INBOX"

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_security: str = "ssl"  # "ssl" or "starttls"

    poll_interval_minutes: int = 5
    only_unread: bool = True
    search_since_days: int = 7
    allowed_senders: list[str] = field(default_factory=list)
    subject_keywords: list[str] = field(default_factory=list)
    require_approval: bool = False
    cc_address: str = ""

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(ENV_FILE, override=True)
        env = os.environ.get
        return cls(
            anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
            claude_model=env("CLAUDE_MODEL", "") or "claude-opus-5",
            email_address=env("EMAIL_ADDRESS", ""),
            email_password=env("EMAIL_PASSWORD", ""),
            imap_host=env("IMAP_HOST", "") or "imap.gmail.com",
            imap_port=int(env("IMAP_PORT", "") or 993),
            imap_folder=env("IMAP_FOLDER", "") or "INBOX",
            smtp_host=env("SMTP_HOST", "") or "smtp.gmail.com",
            smtp_port=int(env("SMTP_PORT", "") or 465),
            smtp_security=(env("SMTP_SECURITY", "") or "ssl").lower(),
            poll_interval_minutes=int(env("POLL_INTERVAL_MINUTES", "") or 5),
            only_unread=_bool(env("ONLY_UNREAD"), True),
            search_since_days=int(env("SEARCH_SINCE_DAYS", "") or 7),
            allowed_senders=_list(env("ALLOWED_SENDERS")),
            subject_keywords=_list(env("SUBJECT_KEYWORDS")),
            require_approval=_bool(env("REQUIRE_APPROVAL"), False),
            cc_address=env("CC_ADDRESS", ""),
        )

    def missing(self) -> list[str]:
        """Names of required settings that are empty."""
        required = {
            "EMAIL_ADDRESS": self.email_address,
            "EMAIL_PASSWORD": self.email_password,
            "IMAP_HOST": self.imap_host,
            "SMTP_HOST": self.smtp_host,
        }
        missing = [k for k, v in required.items() if not v]
        if not self.anthropic_api_key and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            missing.append("ANTHROPIC_API_KEY")
        return missing


def write_env(values: dict[str, str]) -> None:
    """Create or update keys in the .env file, keeping any other lines."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in remaining and not line.lstrip().startswith("#"):
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in remaining.items())
    ENV_FILE.write_text("\n".join(out) + "\n")
    try:
        ENV_FILE.chmod(0o600)
    except OSError:
        pass
