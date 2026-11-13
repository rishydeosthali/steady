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
    channel: str = "whatsapp"
    slack_bot_token: str = field(default="", repr=False)
    slack_signing_secret: str = field(default="", repr=False)
    slack_team_id: str = ""
    slack_senior_user_id: str = ""
    slack_caregiver_user_id: str = ""

    @property
    def recipient(self):
        return self.slack_senior_user_id if self.channel == "slack" else self.mom_whatsapp_to


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from ``.env`` plus the process environment.

    Raises ``ConfigError`` listing missing variable names. Nothing is
    hardcoded; there are no fallbacks for the required values.
    """
    load_dotenv(env_file or REPO_ROOT / ".env", override=False)

    simulation = os.environ.get("STEADY_SIMULATION", "").strip() == "1"
    channel = os.environ.get("STEADY_CHANNEL", "whatsapp").strip().lower()
    if channel not in {"whatsapp", "slack"}:
        raise ConfigError("STEADY_CHANNEL must be whatsapp or slack")
    if channel == "slack" and not simulation:
        raise ConfigError("Slack currently requires STEADY_SIMULATION=1")
    required = [name for name in REQUIRED_VARS if not (simulation and name.startswith("NESSIE_"))]
    if channel == "slack":
        required = ["OPENAI_API_KEY", "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET",
                    "SLACK_TEAM_ID", "SLACK_SENIOR_USER_ID"]
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigError(
            "Missing required settings in .env: " + ", ".join(missing) + ". "
            "Copy .env.example to .env and fill them in."
        )

    def get(name: str) -> str:
        return os.environ[name].strip()

    if channel == "slack":
        if not get("SLACK_TEAM_ID").startswith("T") or not get("SLACK_SENIOR_USER_ID").startswith(("U", "W")):
            raise ConfigError("SLACK_TEAM_ID and SLACK_SENIOR_USER_ID must be Slack IDs")
        caregiver = os.environ.get("SLACK_CAREGIVER_USER_ID", "").strip()
        if caregiver and (not caregiver.startswith(("U", "W")) or caregiver == get("SLACK_SENIOR_USER_ID")):
            raise ConfigError("SLACK_CAREGIVER_USER_ID must identify a different Slack member")

    return Settings(
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", "").strip(),
        twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", "").strip(),
        twilio_whatsapp_from=os.environ.get("TWILIO_WHATSAPP_FROM", "").strip(),
        mom_whatsapp_to=os.environ.get("MOM_WHATSAPP_TO", "").strip(),
        twilio_content_sid=os.environ.get("TWILIO_CONTENT_SID", "").strip(),
        nessie_api_key=os.environ.get("NESSIE_API_KEY", "") if simulation else get("NESSIE_API_KEY"),
        nessie_base_url=os.environ.get("NESSIE_BASE_URL", "").rstrip("/") if simulation else get("NESSIE_BASE_URL").rstrip("/"),
        openai_api_key=get("OPENAI_API_KEY"),
        openai_model=os.environ.get("OPENAI_MODEL", "").strip() or "gpt-4o-mini",
        public_base_url=(os.environ.get("PUBLIC_BASE_URL", "").strip()
                         or os.environ.get("RENDER_EXTERNAL_URL", "").strip()).rstrip("/"),
        allow_any_sender=os.environ.get("ALLOW_ANY_SENDER", "").strip() == "1",
        customer_first_name=os.environ.get("STEADY_CUSTOMER_NAME", "").strip() or "Margaret",
        content_variables=_content_variables(),
        channel=channel,
        slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", "").strip(),
        slack_signing_secret=os.environ.get("SLACK_SIGNING_SECRET", "").strip(),
        slack_team_id=os.environ.get("SLACK_TEAM_ID", "").strip(),
        slack_senior_user_id=os.environ.get("SLACK_SENIOR_USER_ID", "").strip(),
        slack_caregiver_user_id=os.environ.get("SLACK_CAREGIVER_USER_ID", "").strip(),
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
