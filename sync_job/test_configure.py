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
    _prompt_schedule,
    _write_yaml,
    _write_tfvars,
    _build_cron,
    _describe_schedule,
    _parse_hhmm,
    _validate_schedule_value,
    DEFAULT_SCHEDULE,
    ON_DEMAND,
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

    def test_optional_field_returns_empty_on_blank_input(self):
        """alert_email is optional — blank input should return '' not loop."""
        f = field("alert_email")
        with patch("builtins.input", return_value=""):
            result = _prompt(f, None, non_interactive=False)
        assert result == ""

    def test_optional_field_accepts_valid_email(self):
        f = field("alert_email")
        with patch("builtins.input", return_value="ops@example.com"):
            result = _prompt(f, None, non_interactive=False)
        assert result == "ops@example.com"

    def test_optional_field_rejects_invalid_email(self):
        """Invalid email should prompt again; second input is blank (skip)."""
        f = field("alert_email")
        with patch("builtins.input", side_effect=["not-an-email", ""]):
            result = _prompt(f, None, non_interactive=False)
        assert result == ""

    def test_non_interactive_optional_field_returns_empty_when_missing(self):
        """Optional field should return '' in non-interactive mode when not set."""
        f = field("alert_email")
        result = _prompt(f, None, non_interactive=True)
        assert result == ""

    def test_non_interactive_optional_field_validates_when_set(self):
        """Even optional fields must pass validation when a value is present."""
        f = field("alert_email")
        with pytest.raises(SystemExit) as exc_info:
            _prompt(f, "not-an-email", non_interactive=True)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _write_tfvars
# ---------------------------------------------------------------------------

class TestWriteTfvars:

    def _read(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def test_writes_all_required_fields(self):
        config = {
            "prod_project_id": "my-prod",
            "prod_instance_name": "prod-db",
            "nonprod_project_id": "my-nonprod",
            "nonprod_instance_name": "nonprod-db",
            "region": "us-central1",
            "job_name": "cloudsql-sync",
            "schedule": "0 2 * * *",
            "timezone": "UTC",
        }
        with tempfile.NamedTemporaryFile(suffix=".tfvars", delete=False) as f:
            path = f.name
        try:
            _write_tfvars(path, config)
            content = self._read(path)
            assert 'prod_project_id       = "my-prod"' in content
            assert 'nonprod_project_id    = "my-nonprod"' in content
            assert 'region     = "us-central1"' in content
            assert 'container_image = "gcr.io/my-nonprod/cloudsql-sync:latest"' in content
        finally:
            os.unlink(path)

    def test_writes_alert_email_when_set(self):
        config = {
            "prod_project_id": "my-prod",
            "prod_instance_name": "prod-db",
            "nonprod_project_id": "my-nonprod",
            "nonprod_instance_name": "nonprod-db",
            "region": "us-central1",
            "job_name": "cloudsql-sync",
            "schedule": "0 2 * * *",
            "timezone": "UTC",
            "alert_email": "ops@example.com",
        }
        with tempfile.NamedTemporaryFile(suffix=".tfvars", delete=False) as f:
            path = f.name
        try:
            _write_tfvars(path, config)
            content = self._read(path)
            assert 'alert_email = "ops@example.com"' in content
        finally:
            os.unlink(path)

    def test_comments_out_alert_email_when_missing(self):
        config = {
            "prod_project_id": "my-prod",
            "prod_instance_name": "prod-db",
            "nonprod_project_id": "my-nonprod",
            "nonprod_instance_name": "nonprod-db",
            "region": "us-central1",
            "job_name": "cloudsql-sync",
            "schedule": "0 2 * * *",
            "timezone": "UTC",
            "alert_email": "",
        }
        with tempfile.NamedTemporaryFile(suffix=".tfvars", delete=False) as f:
            path = f.name
        try:
            _write_tfvars(path, config)
            content = self._read(path)
            assert '# alert_email' in content
            assert 'alert_email = ""' not in content
        finally:
            os.unlink(path)

    def test_image_uri_uses_job_name(self):
        config = {
            "prod_project_id": "my-prod",
            "prod_instance_name": "prod-db",
            "nonprod_project_id": "my-nonprod",
            "nonprod_instance_name": "nonprod-db",
            "region": "us-central1",
            "job_name": "my-custom-job",
            "schedule": "0 2 * * *",
            "timezone": "UTC",
        }
        with tempfile.NamedTemporaryFile(suffix=".tfvars", delete=False) as f:
            path = f.name
        try:
            _write_tfvars(path, config)
            content = self._read(path)
            assert "my-custom-job" in content
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# alert_email field validator
# ---------------------------------------------------------------------------

class TestAlertEmailValidator:

    def test_valid_emails(self):
        f = field("alert_email")
        for email in ["user@example.com", "ops+alerts@company.co.uk", "a@b.c"]:
            assert f["validate"](email) is None, f"Expected {email!r} to be valid"

    def test_invalid_emails(self):
        f = field("alert_email")
        for email in ["notanemail", "@nodomain", "no-at-sign"]:
            assert f["validate"](email) is not None, f"Expected {email!r} to be invalid"

    def test_empty_string_passes(self):
        """Empty string is valid — alert_email is optional."""
        f = field("alert_email")
        assert f["validate"]("") is None


# ---------------------------------------------------------------------------
# use_latest_existing_backup field + tfvars emission
# ---------------------------------------------------------------------------

class TestUseLatestExistingBackup:

    @pytest.mark.parametrize("value", ["true", "false", "TRUE", "False"])
    def test_validator_accepts_bools(self, value):
        assert field("use_latest_existing_backup")["validate"](value) is None

    @pytest.mark.parametrize("value", ["yes", "1", "maybe", "", "  "])
    def test_validator_rejects_non_bools(self, value):
        assert field("use_latest_existing_backup")["validate"](value) is not None

    def _base_config(self, **overrides):
        cfg = {
            "prod_project_id": "my-prod",
            "prod_instance_name": "prod-db",
            "nonprod_project_id": "my-nonprod",
            "nonprod_instance_name": "nonprod-db",
            "region": "us-central1",
            "job_name": "cloudsql-sync",
            "schedule": "0 2 * * *",
            "timezone": "UTC",
        }
        cfg.update(overrides)
        return cfg

    def test_tfvars_emits_true_unquoted(self):
        cfg = self._base_config(use_latest_existing_backup="true")
        with tempfile.NamedTemporaryFile(suffix=".tfvars", delete=False) as f:
            path = f.name
        try:
            _write_tfvars(path, cfg)
            content = open(path).read()
            assert "use_latest_existing_backup = true" in content
        finally:
            os.unlink(path)

    def test_tfvars_emits_false_unquoted_by_default(self):
        cfg = self._base_config()  # field absent → defaults to false
        with tempfile.NamedTemporaryFile(suffix=".tfvars", delete=False) as f:
            path = f.name
        try:
            _write_tfvars(path, cfg)
            content = open(path).read()
            assert "use_latest_existing_backup = false" in content
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Schedule builder helpers
# ---------------------------------------------------------------------------

class TestParseHHMM:

    @pytest.mark.parametrize("text,expected", [
        ("00:00", (0, 0)),
        ("2:30", (2, 30)),
        ("02:05", (2, 5)),
        ("23:59", (23, 59)),
        ("9:00", (9, 0)),
    ])
    def test_valid(self, text, expected):
        assert _parse_hhmm(text) == expected

    @pytest.mark.parametrize("text", ["24:00", "12:60", "noon", "", "2", "2:5:1", "-1:00"])
    def test_invalid(self, text):
        assert _parse_hhmm(text) is None


class TestBuildCron:

    def test_daily(self):
        assert _build_cron(2, 0) == "0 2 * * *"

    def test_daily_with_minutes(self):
        assert _build_cron(2, 30) == "30 2 * * *"

    def test_weekly_saturday(self):
        # Saturday = 6
        assert _build_cron(23, 0, weekday=6) == "0 23 * * 6"

    def test_weekdays(self):
        assert _build_cron(2, 0, weekdays_only=True) == "0 2 * * 1-5"


class TestValidateScheduleValue:

    def test_accepts_on_demand(self):
        assert _validate_schedule_value("on-demand") is None
        assert _validate_schedule_value("ON-DEMAND") is None

    def test_accepts_cron(self):
        assert _validate_schedule_value("0 23 * * 6") is None

    @pytest.mark.parametrize("v", ["weekly", "every night", "", "0 2 * *"])
    def test_rejects_other(self, v):
        assert _validate_schedule_value(v) is not None


class TestDescribeSchedule:

    def test_on_demand(self):
        assert "on-demand" in _describe_schedule("on-demand")
        assert "on-demand" in _describe_schedule("")

    def test_cron(self):
        assert "0 23 * * 6" in _describe_schedule("0 23 * * 6")


class TestDefaultSchedule:

    def test_default_is_saturday_night(self):
        # 0 23 * * 6  →  minute 0, hour 23, any day-of-month/month, Saturday(6)
        assert DEFAULT_SCHEDULE == "0 23 * * 6"


# ---------------------------------------------------------------------------
# _prompt_schedule (interactive)
# ---------------------------------------------------------------------------

class TestPromptSchedule:

    def test_non_interactive_returns_current(self):
        assert _prompt_schedule("0 2 * * *", non_interactive=True) == "0 2 * * *"

    def test_non_interactive_defaults_when_empty(self):
        assert _prompt_schedule(None, non_interactive=True) == DEFAULT_SCHEDULE

    def test_non_interactive_invalid_exits(self):
        with pytest.raises(SystemExit):
            _prompt_schedule("not-a-schedule", non_interactive=True)

    def test_keep_current_on_yes(self):
        with patch("builtins.input", side_effect=["y"]):
            assert _prompt_schedule("0 9 * * 1", non_interactive=False) == "0 9 * * 1"

    def test_weekly_default_path(self):
        # No current; choose weekly (1), Saturday, 23:00 → default.
        with patch("builtins.input", side_effect=["1", "saturday", "23:00"]):
            assert _prompt_schedule(None, non_interactive=False) == "0 23 * * 6"

    def test_weekly_other_day_and_time(self):
        with patch("builtins.input", side_effect=["1", "monday", "06:30"]):
            assert _prompt_schedule(None, non_interactive=False) == "30 6 * * 1"

    def test_daily_with_time(self):
        with patch("builtins.input", side_effect=["2", "02:00"]):
            assert _prompt_schedule(None, non_interactive=False) == "0 2 * * *"

    def test_weekdays(self):
        with patch("builtins.input", side_effect=["3", "07:15"]):
            assert _prompt_schedule(None, non_interactive=False) == "15 7 * * 1-5"

    def test_on_demand(self):
        with patch("builtins.input", side_effect=["4"]):
            assert _prompt_schedule(None, non_interactive=False) == ON_DEMAND

    def test_custom_cron(self):
        with patch("builtins.input", side_effect=["5", "*/30 * * * *"]):
            assert _prompt_schedule(None, non_interactive=False) == "*/30 * * * *"

    def test_invalid_time_reprompts(self):
        # daily → bad time → good time
        with patch("builtins.input", side_effect=["2", "25:00", "03:00"]):
            assert _prompt_schedule(None, non_interactive=False) == "0 3 * * *"

    def test_invalid_weekday_reprompts(self):
        with patch("builtins.input", side_effect=["1", "funday", "saturday", "23:00"]):
            assert _prompt_schedule(None, non_interactive=False) == "0 23 * * 6"
