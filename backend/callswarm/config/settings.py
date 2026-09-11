"""Typed application settings.

Every variable in ``.env.example`` is loaded here with a safe default. The
defaults encode the default-off call posture: no live calls, the fake call
provider, the fixture research provider and an empty recipient allow-list.

Secrets are ``SecretStr`` so they can never be serialized by accident; nothing
in this module exposes their values.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CallProviderName = Literal["fake", "calle"]
# "live" is an alias for the one live implementation, "gemini_grounded".
ResearchProviderName = Literal["fixture", "gemini_grounded", "live"]

DEFAULT_REASONING_LEAK_MARKERS: tuple[str, ...] = (
    "<thinking>",
    "</thinking>",
    "<think>",
    "</think>",
    "<scratchpad>",
    "chain of thought",
    "chain-of-thought",
    "let me think",
)


DEFAULT_PROHIBITED_AGENT_PURPOSES: tuple[str, ...] = (
    "medical diagnosis",
    "legal advice",
    "financial trading",
    "credential collection",
    "password collection",
    "otp collection",
    "one-time password",
    "impersonation",
    "debt collection",
    "political persuasion",
)


def _split_csv(value: object) -> object:
    """Parse a comma-separated environment value into a list of stripped items."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


# The receiver route in ``api/webhooks.py``; the secret is the final path segment.
WEBHOOK_PATH_PREFIX = "/calle/webhook"


class Settings(BaseSettings):
    """Application configuration loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Reasoning provider (Gemini) ---------------------------------------
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash"
    google_genai_use_vertexai: bool = False
    google_cloud_project: str | None = None
    google_cloud_location: str | None = None

    # --- CALL-E --------------------------------------------------------------
    calle_api_key: SecretStr | None = None
    calle_api_base_url: str = "https://api.heycall-e.com"
    calle_webhook_url: str | None = None
    calle_webhook_secret: SecretStr | None = None
    calle_live_calls_enabled: bool = False
    # HTTP timeout for one CALL-E API request, and the polling profile used by
    # ``CalleProvider.wait_for_terminal`` when no webhook URL is configured.
    calle_request_timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)
    calle_poll_interval_seconds: float = Field(default=2.0, gt=0.0, le=60.0)
    calle_poll_timeout_seconds: float = Field(default=900.0, gt=0.0, le=7200.0)
    call_provider: CallProviderName = "fake"
    call_max_per_mission: int = Field(default=5, ge=0)
    call_quiet_hours_start: str = "21:00"
    call_quiet_hours_end: str = "09:00"
    call_allowed_recipients: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Minutes a PENDING approval stays decidable before ``expire_stale`` marks it EXPIRED.
    approval_ttl_minutes: int = Field(default=60, ge=1)

    # --- Call value scoring (deterministic; see calls/scoring.py) --------------
    # Intents scoring below this priority are not selected.
    call_min_priority: float = Field(default=0.35, ge=0.0, le=1.0)
    # Positive-factor weights sum to 1.0 so the positive term lies in [0, 1];
    # the two penalties are subtracted and the result is clamped to [0, 1].
    call_weight_mission_impact: float = Field(default=0.25, ge=0.0)
    call_weight_uncertainty: float = Field(default=0.15, ge=0.0)
    call_weight_time_sensitivity: float = Field(default=0.10, ge=0.0)
    call_weight_expected_value: float = Field(default=0.20, ge=0.0)
    call_weight_strategy_change_potential: float = Field(default=0.15, ge=0.0)
    call_weight_evidence_importance: float = Field(default=0.15, ge=0.0)
    call_weight_redundancy: float = Field(default=0.50, ge=0.0)
    call_weight_call_cost: float = Field(default=0.30, ge=0.0)

    # --- Research provider ---------------------------------------------------
    research_provider: ResearchProviderName = "fixture"
    search_api_key: SecretStr | None = None
    search_api_endpoint: str | None = None
    # Directory scanned (recursively) for ``research.json`` fixtures.
    research_fixture_dir: str = "../scenarios"
    # Public-page fetch limits: timeout, size cap, and the User-Agent token that
    # robots.txt rules are matched against.
    research_fetch_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    research_fetch_max_bytes: int = Field(default=512_000, ge=1_000)
    research_user_agent: str = "CallSwarm/0.1 (+research; respects robots.txt)"

    # --- Orchestration -------------------------------------------------------
    # Clarification questions at or above this importance are asked; the rest
    # become recorded assumptions.
    clarification_importance_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    # Maximum number of generated agents executing at the same time.
    agent_concurrency: int = Field(default=4, ge=1)
    # Maximum model turns one agent run may take before it is failed.
    agent_max_turns: int = Field(default=4, ge=1)
    # Token-overlap ratio above which two strategies or two agent responsibilities
    # count as duplicates.
    strategy_overlap_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    agent_overlap_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    # Purposes no generated agent may own. Matched case-insensitively against the
    # agent's objective, role and ownership statement.
    prohibited_agent_purposes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_PROHIBITED_AGENT_PURPOSES)
    )
    # Replan loop guard: total decisions per mission, and how many consecutive
    # decisions may repeat the same action on the same target before code
    # forces PROCEED_TO_OPTIMIZATION.
    replan_max_per_mission: int = Field(default=12, ge=1)
    replan_max_consecutive_same: int = Field(default=3, ge=1)

    # --- Persistence ---------------------------------------------------------
    database_url: SecretStr = SecretStr("sqlite+aiosqlite:///./callswarm.db")

    # --- Server --------------------------------------------------------------
    backend_host: str = "127.0.0.1"
    backend_port: int = Field(default=8000, ge=1, le=65535)
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # --- Output sanitization -------------------------------------------------
    # Phrases whose presence in any outbound text marks a chain-of-thought leak.
    reasoning_leak_markers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_REASONING_LEAK_MARKERS)
    )

    @field_validator("call_allowed_recipients", "cors_allowed_origins", mode="before")
    @classmethod
    def _parse_csv_lists(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("reasoning_leak_markers", mode="before")
    @classmethod
    def _parse_markers(cls, value: object) -> object:
        # An empty value must never disable the guard: fall back to the defaults.
        parsed = _split_csv(value)
        if isinstance(parsed, list) and not parsed:
            return list(DEFAULT_REASONING_LEAK_MARKERS)
        return parsed

    @field_validator("prohibited_agent_purposes", mode="before")
    @classmethod
    def _parse_prohibited_purposes(cls, value: object) -> object:
        # Same rule: an empty value must never disable the boundary check.
        parsed = _split_csv(value)
        if isinstance(parsed, list) and not parsed:
            return list(DEFAULT_PROHIBITED_AGENT_PURPOSES)
        return parsed

    @field_validator(
        "gemini_api_key", "calle_api_key", "calle_webhook_secret", "search_api_key", mode="before"
    )
    @classmethod
    def _empty_secret_is_none(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator(
        "calle_webhook_url",
        "google_cloud_project",
        "google_cloud_location",
        "search_api_endpoint",
        mode="before",
    )
    @classmethod
    def _empty_string_is_none(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @model_validator(mode="after")
    def _webhook_url_must_carry_the_secret(self) -> Settings:
        """With ``CALLE_WEBHOOK_URL`` set the provider never polls: the terminal
        state arrives only by webhook. A URL the receiver would reject (no
        secret, or a path token other than the secret) would strand every
        run in ``CALL_EXECUTION_RUNNING``, so refuse to start instead."""
        if self.calle_webhook_url is None:
            return self
        secret = self.calle_webhook_secret.get_secret_value() if self.calle_webhook_secret else ""
        if not secret:
            raise ValueError(
                "CALLE_WEBHOOK_URL is set but CALLE_WEBHOOK_SECRET is empty; the receiver "
                "would reject every delivery and runs would never finish"
            )
        path = urlsplit(self.calle_webhook_url).path.rstrip("/")
        if path != f"{WEBHOOK_PATH_PREFIX}/{secret}":
            raise ValueError(
                f"CALLE_WEBHOOK_URL path must end with {WEBHOOK_PATH_PREFIX}/<secret> where "
                "<secret> is CALLE_WEBHOOK_SECRET; the receiver would reject every delivery "
                "and runs would never finish"
            )
        return self

    # --- Derived, secret-free views -----------------------------------------
    @property
    def llm_configured(self) -> bool:
        """True when a Gemini auth path is present. Never reveals the key."""
        if self.gemini_api_key is not None and self.gemini_api_key.get_secret_value():
            return True
        return self.google_genai_use_vertexai and bool(self.google_cloud_project)

    @property
    def database_kind(self) -> str:
        """The database dialect name only (e.g. ``sqlite``), never the URL."""
        url = self.database_url.get_secret_value()
        return url.split(":", 1)[0].split("+", 1)[0] if url else "unknown"

    @property
    def calle_configured(self) -> bool:
        return self.calle_api_key is not None and bool(self.calle_api_key.get_secret_value())

    def secret_values(self) -> list[str]:
        """Every configured secret value. Used only by tests to assert non-exposure."""
        values: list[str] = []
        for secret in (
            self.gemini_api_key,
            self.calle_api_key,
            self.calle_webhook_secret,
            self.search_api_key,
            self.database_url,
        ):
            if secret is not None and secret.get_secret_value():
                values.append(secret.get_secret_value())
        return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, loaded once."""
    return Settings()
