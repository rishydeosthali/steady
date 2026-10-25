"""Settings loaded from the environment (``.env`` at the repo root).

Secrets are never printed or logged. ``Settings`` hides them from ``repr`` and
``ConfigError`` only ever names the variables that are missing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_VARS = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_WHATSAPP_FROM",
    "MOM_WHATSAPP_TO",
    "TWILIO_CONTENT_SID",
    "NESSIE_API_KEY",
    "NESSIE_BASE_URL",
    "OPENAI_API_KEY",
)


class ConfigError(RuntimeError):
    """Raised when required settings are missing. Names only, never values."""


@dataclass(frozen=True)
class Settings:
    twilio_account_sid: str = field(repr=False)
    twilio_auth_token: str = field(repr=False)
    twilio_whatsapp_from: str
    mom_whatsapp_to: str
    twilio_content_sid: str
    nessie_api_key: str = field(repr=False)
    nessie_base_url: str
    openai_api_key: str = field(repr=False)
    # Optional, non-secret.
    openai_model: str = "gpt-4o-mini"
    public_base_url: str = ""
    allow_any_sender: bool = False
    customer_first_name: str = "Margaret"
    content_variables: dict[str, str] = field(default_factory=dict)


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from ``.env`` plus the process environment.

    Raises ``ConfigError`` listing missing variable names. Nothing is
    hardcoded; there are no fallbacks for the required values.
    """
    load_dotenv(env_file or REPO_ROOT / ".env", override=False)

    missing = [name for name in REQUIRED_VARS if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigError(
            "Missing required settings in .env: " + ", ".join(missing) + ". "
            "Copy .env.example to .env and fill them in."
        )

    def get(name: str) -> str:
        return os.environ[name].strip()

    return Settings(
        twilio_account_sid=get("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=get("TWILIO_AUTH_TOKEN"),
        twilio_whatsapp_from=get("TWILIO_WHATSAPP_FROM"),
        mom_whatsapp_to=get("MOM_WHATSAPP_TO"),
        twilio_content_sid=get("TWILIO_CONTENT_SID"),
        nessie_api_key=get("NESSIE_API_KEY"),
        nessie_base_url=get("NESSIE_BASE_URL").rstrip("/"),
        openai_api_key=get("OPENAI_API_KEY"),
        openai_model=os.environ.get("OPENAI_MODEL", "").strip() or "gpt-4o-mini",
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        allow_any_sender=os.environ.get("ALLOW_ANY_SENDER", "").strip() == "1",
        customer_first_name=os.environ.get("STEADY_CUSTOMER_NAME", "").strip() or "Margaret",
        content_variables=_content_variables(),
    )


def _content_variables() -> dict[str, str]:
    """Optional JSON object for the sandbox template, such as {"1":"12/1","2":"3pm"}."""
    raw = os.environ.get("TWILIO_CONTENT_VARIABLES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ConfigError("TWILIO_CONTENT_VARIABLES is not valid JSON.") from None
    if not isinstance(parsed, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
        raise ConfigError("TWILIO_CONTENT_VARIABLES must be a JSON object of strings.")
    return parsed
