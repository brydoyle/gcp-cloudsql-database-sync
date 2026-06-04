"""
Unit tests for configure.py.

Run with:  pytest sync_job/test_configure.py -v
"""

import os
import sys
import tempfile
from unittest.mock import patch

import pytest

import configure
from configure import (
    _check_prod_ne_nonprod,
    _load_yaml,
    _prompt,
    _write_yaml,
    FIELDS,
    GCP_REGIONS,
    _PROJECT_ID_RE,
    _INSTANCE_NAME_RE,
    _CRON_RE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def field(key: str) -> dict:
    """Return a FIELDS entry by key."""
    return next(f for f in FIELDS if f["key"] == key)


def tmp_yaml(content: str) -> str:
    """Write content to a temp file and return the path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(content)
    f.close()
    return f.name


# ---------------------------------------------------------------------------
# Regex validators
# ---------------------------------------------------------------------------

class TestProjectIdRegex:

    @pytest.mark.parametrize("value", [
        "abcdef",                        # 6 chars minimum
        "my-prod-project",               # typical
        "proj123",                       # digits
        "a" + "b" * 28 + "c",           # 30 chars maximum
    ])
    def test_valid(self, value):
        assert _PROJECT_ID_RE.match(value)

    @pytest.mark.parametrize("value", [
        "abcde",                         # 5 chars — too short
        "1starts-digit",                 # starts with digit
        "-starts-hyphen",                # starts with hyphen
        "ends-hyphen-",                  # trailing hyphen
        "has_underscore",                # underscore
        "HAS_UPPER",                     # uppercase
        "has spaces",                    # space
        "has\nnewline",                  # newline — log injection
        "a" * 31,                        # 31 chars — too long
    ])
    def test_invalid(self, value):
        assert not _PROJECT_ID_RE.match(value)


class TestInstanceNameRegex:

    @pytest.mark.parametrize("value", [
        "a",                             # 1 char
        "prod-db",                       # typical
        "db01",                          # digits
        "a" + "b" * 96 + "c",           # 98 chars maximum
    ])
    def test_valid(self, value):
        assert _INSTANCE_NAME_RE.match(value)

    @pytest.mark.parametrize("value", [
        "1starts-digit",
        "-starts-hyphen",
        "ends-hyphen-",
        "has_underscore",
        "HAS_UPPER",
        "has\nnewline",
        "a" * 99,                        # too long
    ])
    def test_invalid(self, value):
        assert not _INSTANCE_NAME_RE.match(value)


class TestCronRegex:

    @pytest.mark.parametrize("value", [
        "0 2 * * *",
        "*/15 * * * *",
        "0 0 1 1 *",
        "30 6 * * 1-5",
    ])
    def test_valid(self, value):
        assert _CRON_RE.match(value)

    @pytest.mark.parametrize("value", [
        "0 2 * *",          # only 4 fields
        "0 2 * * * *",      # 6 fields
        "not a cron",
        "",
    ])
    def test_invalid(self, value):
        assert not _CRON_RE.match(value)


# ---------------------------------------------------------------------------
# Field validators
# ---------------------------------------------------------------------------

class TestFieldValidators:

    def test_prod_project_id_valid(self):
        assert field("prod_project_id")["validate"]("my-prod-project") is None

    def test_prod_project_id_invalid(self):
        assert field("prod_project_id")["validate"]("UPPER") is not None

    def test_region_valid(self):
        assert field("region")["validate"]("us-central1") is None

    def test_region_invalid(self):
        assert field("region")["validate"]("not-a-region") is not None

    def test_schedule_valid(self):
        assert field("schedule")["validate"]("0 2 * * *") is None

    def test_schedule_invalid(self):
        assert field("schedule")["validate"]("every night") is not None

    def test_timezone_always_passes(self):
        # Timezone validator is permissive — GCP enforces it
        assert field("timezone")["validate"]("anything") is None

    def test_job_name_valid(self):
        assert field("job_name")["validate"]("cloudsql-sync") is None

    def test_job_name_invalid_uppercase(self):
        assert field("job_name")["validate"]("CloudSQL-Sync") is not None

    def test_job_name_too_long(self):
        assert field("job_name")["validate"]("a" * 50) is not None


# ---------------------------------------------------------------------------
# _load_yaml
# ---------------------------------------------------------------------------

class TestLoadYaml:

    def test_loads_simple_key_value(self):
        path = tmp_yaml("prod_project_id: my-prod\n")
        try:
            cfg = _load_yaml(path)
            assert cfg["prod_project_id"] == "my-prod"
        finally:
            os.unlink(path)

    def test_strips_quotes_from_values(self):
        path = tmp_yaml('schedule: "0 2 * * *"\n')
        try:
            cfg = _load_yaml(path)
            assert cfg["schedule"] == "0 2 * * *"
        finally:
            os.unlink(path)

    def test_strips_single_quotes(self):
        path = tmp_yaml("region: 'us-central1'\n")
        try:
            cfg = _load_yaml(path)
            assert cfg["region"] == "us-central1"
        finally:
            os.unlink(path)

    def test_ignores_comments(self):
        path = tmp_yaml("# this is a comment\nprod_project_id: my-proj\n")
        try:
            cfg = _load_yaml(path)
            assert "prod_project_id" in cfg
            assert len(cfg) == 1
        finally:
            os.unlink(path)

    def test_returns_empty_dict_for_missing_file(self):
        assert _load_yaml("/nonexistent/path/config.yaml") == {}

    def test_returns_empty_dict_for_empty_file(self):
        path = tmp_yaml("")
        try:
            assert _load_yaml(path) == {}
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# _write_yaml / round-trip
# ---------------------------------------------------------------------------

class TestWriteYaml:

    def test_round_trip(self):
        config = {
            "prod_project_id": "my-prod",
            "schedule": "0 2 * * *",
            "region": "us-central1",
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            path = f.name
        try:
            _write_yaml(path, config)
            loaded = _load_yaml(path)
            assert loaded["prod_project_id"] == "my-prod"
            assert loaded["schedule"] == "0 2 * * *"
            assert loaded["region"] == "us-central1"
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# _check_prod_ne_nonprod
# ---------------------------------------------------------------------------

class TestCheckProdNeNonprod:

    def test_same_project_and_instance_exits(self):
        config = {
            "prod_project_id": "same-proj",
            "prod_instance_name": "same-db",
            "nonprod_project_id": "same-proj",
            "nonprod_instance_name": "same-db",
        }
        with pytest.raises(SystemExit) as exc_info:
            _check_prod_ne_nonprod(config)
        assert exc_info.value.code == 1

    def test_same_project_different_instance_passes(self):
        config = {
            "prod_project_id": "same-proj",
            "prod_instance_name": "prod-db",
            "nonprod_project_id": "same-proj",
            "nonprod_instance_name": "nonprod-db",
        }
        _check_prod_ne_nonprod(config)  # should not raise

    def test_different_project_same_instance_passes(self):
        config = {
            "prod_project_id": "prod-proj",
            "prod_instance_name": "same-db",
            "nonprod_project_id": "nonprod-proj",
            "nonprod_instance_name": "same-db",
        }
        _check_prod_ne_nonprod(config)  # should not raise


# ---------------------------------------------------------------------------
# _prompt
# ---------------------------------------------------------------------------

class TestPrompt:

    def test_non_interactive_returns_current_value(self):
        f = field("region")
        result = _prompt(f, "europe-west1", non_interactive=True)
        assert result == "europe-west1"

    def test_non_interactive_returns_default_when_no_current(self):
        f = field("region")
        result = _prompt(f, None, non_interactive=True)
        assert result == "us-central1"

    def test_non_interactive_exits_when_required_missing(self):
        f = field("prod_project_id")
        with pytest.raises(SystemExit) as exc_info:
            _prompt(f, None, non_interactive=True)
        assert exc_info.value.code == 1

    def test_non_interactive_validates_value(self):
        """--non-interactive should reject invalid values in config.yaml."""
        f = field("prod_project_id")
        with pytest.raises(SystemExit) as exc_info:
            _prompt(f, "INVALID_UPPER", non_interactive=True)
        assert exc_info.value.code == 1

    def test_non_interactive_invalid_region_exits(self):
        f = field("region")
        with pytest.raises(SystemExit) as exc_info:
            _prompt(f, "not-a-region", non_interactive=True)
        assert exc_info.value.code == 1

    def test_interactive_accepts_valid_input(self):
        f = field("prod_project_id")
        with patch("builtins.input", return_value="my-prod-proj"):
            result = _prompt(f, None, non_interactive=False)
        assert result == "my-prod-proj"

    def test_interactive_retries_on_invalid_input(self):
        f = field("prod_project_id")
        # First input is invalid, second is valid
        with patch("builtins.input", side_effect=["INVALID", "my-prod-proj"]):
            result = _prompt(f, None, non_interactive=False)
        assert result == "my-prod-proj"

    def test_interactive_uses_default_on_empty_input(self):
        f = field("region")
        with patch("builtins.input", return_value=""):
            result = _prompt(f, None, non_interactive=False)
        assert result == "us-central1"

    def test_interactive_ctrl_c_exits(self):
        f = field("prod_project_id")
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit):
                _prompt(f, None, non_interactive=False)
