"""Application settings, in one place.

Every tunable value (model IDs, budgets, file paths, the fixed "today" date) lives here, so the
rest of the code never reads environment variables directly.

How values are resolved (pydantic-settings), highest priority first:
  1. Real environment variables, e.g. ``SCC_HUB_MODEL=claude-opus-5-5``
  2. The ``.env`` file in the project root
  3. The defaults written below

All our own variables use the ``SCC_`` prefix so they cannot clash with other tools.
The one exception is ``ANTHROPIC_API_KEY``: the Claude Agent SDK (and the Claude Code CLI it
bundles) reads that variable itself, so we keep its standard name.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/scc/config.py -> parents[2] is the project root (the folder containing pyproject.toml).
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Typed settings for the whole app. Create via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_prefix="SCC_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        # .env also holds ANTHROPIC_API_KEY and Chainlit variables; ignore what we don't model.
        extra="ignore",
    )

    # --- Anthropic ----------------------------------------------------------------------------
    # Optional here: the SDK reads ANTHROPIC_API_KEY from the environment by itself. We only
    # model it so the app can fail early with a clear message when it is missing.
    # `validation_alias` makes pydantic read the unprefixed name.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    # --- Models -------------------------------------------------------------------------------
    # The hub and the SQL-writing analyst do the hardest reasoning, so they get Sonnet.
    # The other spokes call deterministic tools and summarise results, so Haiku is enough
    # and keeps cost down.
    hub_model: str = "claude-sonnet-5-5"
    analyst_model: str = "claude-sonnet-5-5"
    spoke_model: str = "claude-haiku-5-5"

    # --- Per-request guard rails ----------------------------------------------------------------
    # Passed to ClaudeAgentOptions on every request, so one runaway question cannot loop
    # forever or spend unbounded money.
    max_turns: int = Field(default=30, ge=1)
    max_budget_usd: float = Field(default=1.00, gt=0)

    # --- Human approval -------------------------------------------------------------------------
    # How long the UI waits for an Approve/Deny click before denying automatically.
    approval_timeout_s: int = Field(default=300, ge=5)

    # --- Data ---------------------------------------------------------------------------------
    # The dataset ends on 2011-12-09. Treating that day as "today" makes answers such as
    # "last 30 days" reproducible and testable. It is stated in every agent prompt.
    as_of_date: date = date(2011, 12, 9)

    data_dir: Path = PROJECT_ROOT / "data"
    uci_url: str = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"

    # --- Live integrations ----------------------------------------------------------------------
    # Frankfurter (ECB exchange rates) needs no key. The old .app domain is kept as a fallback.
    fx_base_url: str = "https://api.frankfurter.dev/v1"
    fx_fallback_url: str = "https://api.frankfurter.app"
    fx_cache_ttl_s: int = Field(default=3600, ge=0)
    http_timeout_s: float = Field(default=10.0, gt=0)

    # --- Optional UI login ----------------------------------------------------------------------
    # When both are set, Chainlit shows a login page. Leave empty for local development.
    app_user: str | None = None
    app_password: SecretStr | None = None

    # --- Derived paths --------------------------------------------------------------------------
    @property
    def duckdb_path(self) -> Path:
        """Analytical database built by ingest (read-only at runtime)."""
        return self.data_dir / "retail.duckdb"

    @property
    def app_db_path(self) -> Path:
        """SQLite database for app state: sessions, memory, approvals, drafts, POs, audit."""
        return self.data_dir / "app.sqlite"

    @property
    def raw_dir(self) -> Path:
        """Where the downloaded UCI zip and extracted Excel file are kept."""
        return self.data_dir / "raw"

    @property
    def login_enabled(self) -> bool:
        return bool(self.app_user and self.app_password)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (built once, then cached).

    Tests that need different values should build ``Settings(...)`` directly, or call
    ``get_settings.cache_clear()`` after changing environment variables.
    """
    return Settings()
