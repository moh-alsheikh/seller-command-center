"""Tests for scc.config. These run offline and never touch the real .env file."""

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from scc.config import PROJECT_ROOT, Settings


def make_settings(**overrides) -> Settings:
    # _env_file=None ignores the developer's real .env, so the tests are deterministic.
    return Settings(_env_file=None, **overrides)


def test_defaults_match_the_plan(monkeypatch):
    for var in ("SCC_HUB_MODEL", "SCC_AS_OF_DATE", "SCC_DATA_DATES", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    s = make_settings()

    # Design rule 6: the reference date is read from the database (scc.data.db.resolve_as_of)
    # unless set explicitly; ingest shifts the data to recent dates by default.
    assert s.as_of_date is None
    assert s.data_dates == "recent"
    assert s.hub_model == "claude-sonnet-5-5"
    assert s.spoke_model == "claude-haiku-5-5"
    assert s.anthropic_api_key is None
    assert s.login_enabled is False


def test_project_root_is_the_repo_root():
    assert (PROJECT_ROOT / "pyproject.toml").is_file()


def test_derived_paths_follow_data_dir(tmp_path: Path):
    s = make_settings(data_dir=tmp_path)
    assert s.duckdb_path == tmp_path / "retail.duckdb"
    assert s.app_db_path == tmp_path / "app.sqlite"
    assert s.raw_dir == tmp_path / "raw"


def test_env_vars_use_scc_prefix(monkeypatch):
    monkeypatch.setenv("SCC_HUB_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("SCC_MAX_BUDGET_USD", "0.25")
    s = make_settings()
    assert s.hub_model == "claude-opus-5-5"
    assert s.max_budget_usd == 0.25


def test_api_key_uses_standard_unprefixed_name(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    s = make_settings()
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(s)  # SecretStr keeps the key out of logs


def test_login_needs_both_user_and_password():
    assert make_settings(app_user="seller").login_enabled is False
    assert make_settings(app_user="seller", app_password="pw").login_enabled is True


@pytest.mark.parametrize("field,value", [("max_turns", 0), ("max_budget_usd", 0)])
def test_guard_rails_reject_non_positive_values(field, value):
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_explicit_as_of_override():
    assert make_settings(as_of_date="2026-11-13").as_of_date == date(2026, 11, 13)
