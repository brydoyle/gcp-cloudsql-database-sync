"""
Unit tests for main.py.

Run with:  pytest sync_job/test_main.py -v
Coverage:  pytest sync_job/test_main.py --cov=sync_job/main --cov-report=term-missing
"""

import http
import io
import logging
import unittest
from unittest.mock import MagicMock, call, patch

import pytest
from googleapiclient.errors import HttpError

import main
from main import (
    Config,
    OperationTimeout,
    SyncError,
    Target,
    _bool_env,
    _extract_op_name,
    _fetch_secret,
    _parse_targets,
    _raise_for_http_error,
    _validate_instance_name,
    _validate_project_id,
    _validate_region,
    acquire_backup,
    create_backup,
    delete_backup,
    get_latest_backup,
    load_config,
    reset_target_password,
    restore_to_target,
    wait_for_operation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_ENV = {
    "PROD_PROJECT_ID": "my-prod-project",
    "PROD_INSTANCE_NAME": "prod-db",
    "NONPROD_PROJECT_ID": "my-nonprod-project",
    "NONPROD_INSTANCE_NAME": "nonprod-db",
    "GCP_REGION": "us-central1",
}

FAST_TARGET = Target(project="my-nonprod-project", instance="nonprod-db")

FAST_CFG = Config(
    prod_project="my-prod-project",
    prod_instance="prod-db",
    nonprod_targets=(FAST_TARGET,),
    region="us-central1",
    poll_interval=0,
    operation_timeout=60,
)


def make_http_error(status: int, reason: str = "error") -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = reason
    return HttpError(resp=resp, content=b"", uri="https://example.com")


def make_service(op_responses: list) -> MagicMock:
    """Build a mock sqladmin service that returns op_responses in sequence
    when operations().get().execute() is called."""
    svc = MagicMock()
    svc.operations.return_value.get.return_value.execute.side_effect = op_responses
    return svc


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:

    def test_happy_path(self):
        with patch.dict("os.environ", VALID_ENV, clear=True):
            cfg = load_config()
        assert cfg.prod_project == "my-prod-project"
        assert cfg.prod_instance == "prod-db"
        assert cfg.nonprod_targets == (Target("my-nonprod-project", "nonprod-db"),)
        assert cfg.poll_interval == 15
        assert cfg.operation_timeout == 7200

    def test_optional_int_overrides(self):
        env = {**VALID_ENV, "POLL_INTERVAL_SECONDS": "30", "OPERATION_TIMEOUT_SECONDS": "3600"}
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.poll_interval == 30
        assert cfg.operation_timeout == 3600

    def test_region_stored_in_config(self):
        env = {**VALID_ENV, "GCP_REGION": "europe-west1"}
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.region == "europe-west1"

    def test_missing_region_rejected(self):
        env = {k: v for k, v in VALID_ENV.items() if k != "GCP_REGION"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_invalid_region_rejected(self):
        env = {**VALID_ENV, "GCP_REGION": "not-a-real-region"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_missing_all_vars_reports_all_errors(self):
        """Should report every missing variable before exiting — not just the first."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_missing_single_var(self):
        env = {k: v for k, v in VALID_ENV.items() if k != "NONPROD_INSTANCE_NAME"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_prod_equals_nonprod_same_project_and_instance(self):
        env = {
            "PROD_PROJECT_ID": "same-project",
            "PROD_INSTANCE_NAME": "same-db",
            "NONPROD_PROJECT_ID": "same-project",
            "NONPROD_INSTANCE_NAME": "same-db",
            "GCP_REGION": "us-central1",
        }
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_same_project_different_instance_is_allowed(self):
        """Same project but different instances should not trigger the guard."""
        env = {
            "PROD_PROJECT_ID": "same-project",
            "PROD_INSTANCE_NAME": "prod-db",
            "NONPROD_PROJECT_ID": "same-project",
            "NONPROD_INSTANCE_NAME": "nonprod-db",
            "GCP_REGION": "us-central1",
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.prod_instance == "prod-db"

    def test_invalid_resource_name_rejected(self):
        env = {**VALID_ENV, "PROD_PROJECT_ID": "INVALID_UPPER_CASE"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_resource_name_with_special_chars_rejected(self):
        env = {**VALID_ENV, "PROD_INSTANCE_NAME": "db; DROP TABLE users;--"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_poll_interval_below_minimum_rejected(self):
        env = {**VALID_ENV, "POLL_INTERVAL_SECONDS": "0"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_poll_interval_above_maximum_rejected(self):
        env = {**VALID_ENV, "POLL_INTERVAL_SECONDS": "9999"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_timeout_below_minimum_rejected(self):
        env = {**VALID_ENV, "OPERATION_TIMEOUT_SECONDS": "10"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_timeout_above_maximum_rejected(self):
        env = {**VALID_ENV, "OPERATION_TIMEOUT_SECONDS": "999999"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_non_integer_poll_interval_rejected(self):
        env = {**VALID_ENV, "POLL_INTERVAL_SECONDS": "fast"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_whitespace_only_value_treated_as_missing(self):
        env = {**VALID_ENV, "PROD_PROJECT_ID": "   "}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1

    def test_multi_target_via_nonprod_targets(self):
        env = {
            "PROD_PROJECT_ID": "my-prod-project",
            "PROD_INSTANCE_NAME": "prod-db",
            "GCP_REGION": "us-central1",
            "NONPROD_TARGETS": "dev-project:dev-db,qa-project:qa-db",
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.nonprod_targets == (
            Target("dev-project", "dev-db"),
            Target("qa-project", "qa-db"),
        )

    def test_nonprod_targets_takes_precedence_over_single(self):
        env = {
            **VALID_ENV,  # has single NONPROD_PROJECT_ID / NONPROD_INSTANCE_NAME
            "NONPROD_TARGETS": "dev-project:dev-db",
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.nonprod_targets == (Target("dev-project", "dev-db"),)

    def test_multi_target_one_equals_prod_rejected(self):
        env = {
            "PROD_PROJECT_ID": "my-prod-project",
            "PROD_INSTANCE_NAME": "prod-db",
            "GCP_REGION": "us-central1",
            "NONPROD_TARGETS": "dev-project:dev-db,my-prod-project:prod-db",
        }
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_config()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _parse_targets
# ---------------------------------------------------------------------------

class TestParseTargets:

    def test_single_fallback(self):
        env = {
            "NONPROD_PROJECT_ID": "my-nonprod-project",
            "NONPROD_INSTANCE_NAME": "nonprod-db",
        }
        with patch.dict("os.environ", env, clear=True):
            errors: list = []
            targets = _parse_targets(errors)
        assert errors == []
        assert targets == (Target("my-nonprod-project", "nonprod-db"),)

    def test_multi_parses_all(self):
        env = {"NONPROD_TARGETS": "dev-project:dev-db, qa-project:qa-db"}
        with patch.dict("os.environ", env, clear=True):
            errors: list = []
            targets = _parse_targets(errors)
        assert errors == []
        assert len(targets) == 2

    def test_malformed_entry_no_colon(self):
        env = {"NONPROD_TARGETS": "dev-project-dev-db"}
        with patch.dict("os.environ", env, clear=True):
            errors: list = []
            _parse_targets(errors)
        assert any("project:instance" in e for e in errors)

    def test_malformed_entry_extra_colon(self):
        env = {"NONPROD_TARGETS": "dev:db:extra"}
        with patch.dict("os.environ", env, clear=True):
            errors: list = []
            _parse_targets(errors)
        assert len(errors) >= 1

    def test_duplicate_targets_rejected(self):
        env = {"NONPROD_TARGETS": "dev-project:dev-db,dev-project:dev-db"}
        with patch.dict("os.environ", env, clear=True):
            errors: list = []
            _parse_targets(errors)
        assert any("Duplicate" in e for e in errors)

    def test_invalid_target_project_reported(self):
        env = {"NONPROD_TARGETS": "BAD_PROJECT:dev-db"}
        with patch.dict("os.environ", env, clear=True):
            errors: list = []
            _parse_targets(errors)
        assert len(errors) >= 1

    def test_empty_entries_skipped(self):
        env = {"NONPROD_TARGETS": "dev-project:dev-db,,qa-project:qa-db,"}
        with patch.dict("os.environ", env, clear=True):
            errors: list = []
            targets = _parse_targets(errors)
        assert errors == []
        assert len(targets) == 2


# ---------------------------------------------------------------------------
# _validate_project_id
# ---------------------------------------------------------------------------

class TestValidateProjectId:

    @pytest.mark.parametrize("name", [
        "my-proj1",           # typical
        "acme-prod",          # typical
        "abcdef",             # 6 chars minimum
        "a" + "b" * 28 + "c", # 30 chars maximum
        "proj123",            # digits in middle
    ])
    def test_valid_project_ids(self, name):
        errors: list = []
        _validate_project_id(name, "LABEL", errors)
        assert errors == [], f"Expected {name!r} to be valid"

    @pytest.mark.parametrize("name", [
        "ab",                  # too short (< 6 chars)
        "abcde",               # 5 chars — still too short
        "1starts-with-digit",  # must start with letter
        "-starts-with-hyphen", # must start with letter
        "ends-with-hyphen-",   # no trailing hyphen
        "has_underscore",      # underscores not allowed
        "has.dot",             # dots not allowed
        "HAS_UPPER",           # uppercase not allowed
        "has spaces",          # spaces not allowed
        "has\nnewline",        # newlines not allowed (log injection)
        "a" * 31,              # too long (> 30 chars)
    ])
    def test_invalid_project_ids(self, name):
        errors: list = []
        _validate_project_id(name, "LABEL", errors)
        assert len(errors) == 1, f"Expected {name!r} to be invalid"

    def test_empty_string_skipped(self):
        errors: list = []
        _validate_project_id("", "LABEL", errors)
        assert errors == []


# ---------------------------------------------------------------------------
# _validate_instance_name
# ---------------------------------------------------------------------------

class TestValidateInstanceName:

    @pytest.mark.parametrize("name", [
        "a",                    # 1 char minimum
        "prod-db",              # typical
        "nonprod-db-01",        # typical with numbers
        "a" + "b" * 96 + "c",  # 98 chars maximum
    ])
    def test_valid_instance_names(self, name):
        errors: list = []
        _validate_instance_name(name, "LABEL", errors)
        assert errors == [], f"Expected {name!r} to be valid"

    @pytest.mark.parametrize("name", [
        "1starts-with-digit",   # must start with letter
        "-starts-with-hyphen",  # must start with letter
        "ends-with-hyphen-",    # no trailing hyphen
        "has_underscore",       # underscores not allowed
        "HAS_UPPER",            # uppercase not allowed
        "has spaces",           # spaces not allowed
        "has\nnewline",         # newlines not allowed
        "a" * 99,               # too long (> 98 chars)
    ])
    def test_invalid_instance_names(self, name):
        errors: list = []
        _validate_instance_name(name, "LABEL", errors)
        assert len(errors) == 1, f"Expected {name!r} to be invalid"

    def test_empty_string_skipped(self):
        errors: list = []
        _validate_instance_name("", "LABEL", errors)
        assert errors == []


# ---------------------------------------------------------------------------
# _validate_region
# ---------------------------------------------------------------------------

class TestValidateRegion:

    @pytest.mark.parametrize("region", [
        "us-central1",
        "europe-west1",
        "asia-east1",
        "us-east4",
        "australia-southeast1",
        "me-west1",
        "africa-south1",
    ])
    def test_valid_regions(self, region):
        errors: list = []
        _validate_region(region, "GCP_REGION", errors)
        assert errors == []

    @pytest.mark.parametrize("region", [
        "us-central",           # missing zone number
        "us-central-1",         # wrong separator format
        "not-a-region",
        "US-CENTRAL1",          # uppercase
        "us central1",          # space
        "us-central1; DROP",    # injection attempt
    ])
    def test_invalid_regions(self, region):
        errors: list = []
        _validate_region(region, "GCP_REGION", errors)
        assert len(errors) == 1

    def test_empty_string_skipped(self):
        errors: list = []
        _validate_region("", "GCP_REGION", errors)
        assert errors == []


# ---------------------------------------------------------------------------
# _extract_op_name
# ---------------------------------------------------------------------------

class TestExtractOpName:

    def test_returns_name_when_present(self):
        assert _extract_op_name({"name": "operations/abc123"}, "ctx") == "operations/abc123"

    def test_raises_sync_error_when_absent(self):
        with pytest.raises(SyncError) as exc_info:
            _extract_op_name({}, "backupRuns.insert")
        assert exc_info.value.exit_code == 4
        assert "backupRuns.insert" in str(exc_info.value)

    def test_raises_sync_error_when_none(self):
        with pytest.raises(SyncError):
            _extract_op_name({"name": None}, "ctx")


# ---------------------------------------------------------------------------
# _raise_for_http_error
# ---------------------------------------------------------------------------

class TestRaiseForHttpError:

    @pytest.mark.parametrize("status,hint_fragment", [
        (400, "version mismatch"),
        (403, "cloudsql.* permissions"),
        (404, "not found"),
        (409, "conflicting operation"),
    ])
    def test_known_statuses_include_hint(self, status, hint_fragment):
        exc = make_http_error(status)
        with pytest.raises(SyncError) as exc_info:
            _raise_for_http_error(exc, "some context")
        assert hint_fragment in str(exc_info.value)
        assert exc_info.value.exit_code == 2

    def test_unknown_status_still_raises(self):
        exc = make_http_error(500, "Internal Server Error")
        with pytest.raises(SyncError) as exc_info:
            _raise_for_http_error(exc, "some context")
        assert "500" in str(exc_info.value)


# ---------------------------------------------------------------------------
# wait_for_operation
# ---------------------------------------------------------------------------

class TestWaitForOperation:

    def test_returns_result_when_done(self):
        done_result = {"status": "DONE", "targetId": "42"}
        svc = make_service([done_result])
        result = wait_for_operation(svc, "proj", "operations/123", FAST_CFG)
        assert result == done_result

    def test_polls_until_done(self):
        responses = [
            {"status": "RUNNING"},
            {"status": "PENDING"},
            {"status": "DONE", "targetId": "99"},
        ]
        svc = make_service(responses)
        with patch("main.time.sleep"):
            result = wait_for_operation(svc, "proj", "operations/123", FAST_CFG)
        assert result["targetId"] == "99"
        assert svc.operations.return_value.get.return_value.execute.call_count == 3

    def test_raises_sync_error_on_operation_error(self):
        done_with_error = {
            "status": "DONE",
            "error": {"errors": [{"code": "INTERNAL", "message": "something went wrong"}]},
        }
        svc = make_service([done_with_error])
        with pytest.raises(SyncError) as exc_info:
            wait_for_operation(svc, "proj", "operations/123", FAST_CFG)
        assert "INTERNAL" in str(exc_info.value)
        assert exc_info.value.exit_code == 2

    def test_raises_operation_timeout(self):
        cfg = Config(**{**FAST_CFG.__dict__, "operation_timeout": 60})
        svc = MagicMock()
        # Make monotonic advance past the deadline immediately on second call.
        start = 1000.0
        with patch("main.time.monotonic", side_effect=[start, start + 61]):
            with patch("main.time.sleep"):
                with pytest.raises(OperationTimeout) as exc_info:
                    wait_for_operation(svc, "proj", "operations/123", cfg)
        assert exc_info.value.exit_code == 3

    def test_raises_sync_error_on_http_error(self):
        svc = MagicMock()
        svc.operations.return_value.get.return_value.execute.side_effect = make_http_error(403)
        with pytest.raises(SyncError) as exc_info:
            wait_for_operation(svc, "proj", "operations/123", FAST_CFG)
        assert exc_info.value.exit_code == 2

    def test_adaptive_sleep_does_not_overshoot(self):
        """Sleep should be capped to remaining time on the last interval."""
        done_result = {"status": "DONE"}
        svc = make_service([{"status": "RUNNING"}, done_result])
        cfg = Config(**{**FAST_CFG.__dict__, "poll_interval": 30, "operation_timeout": 10})

        sleep_calls = []
        start = 1000.0
        # monotonic: deadline check (1000), post-poll sleep calc (1005 — 5s remaining)
        with patch("main.time.monotonic", side_effect=[start, start, start + 5, start + 5, start + 6]):
            with patch("main.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
                try:
                    wait_for_operation(svc, "proj", "operations/123", cfg)
                except (SyncError, OperationTimeout):
                    pass

        # Sleep should be <= 5 (remaining), not 30 (poll_interval)
        for s in sleep_calls:
            assert s <= 5


# ---------------------------------------------------------------------------
# create_backup
# ---------------------------------------------------------------------------

class TestCreateBackup:

    def test_happy_path_returns_backup_id(self):
        svc = MagicMock()
        svc.backupRuns.return_value.insert.return_value.execute.return_value = {
            "name": "operations/backup-op"
        }
        svc.operations.return_value.get.return_value.execute.return_value = {
            "status": "DONE",
            "targetId": "12345",
        }
        backup_id = create_backup(svc, FAST_CFG)
        assert backup_id == 12345

    def test_http_error_raises_sync_error(self):
        svc = MagicMock()
        svc.backupRuns.return_value.insert.return_value.execute.side_effect = make_http_error(409)
        with pytest.raises(SyncError) as exc_info:
            create_backup(svc, FAST_CFG)
        assert "conflicting" in str(exc_info.value).lower()

    def test_missing_target_id_raises_sync_error(self):
        svc = MagicMock()
        svc.backupRuns.return_value.insert.return_value.execute.return_value = {
            "name": "operations/backup-op"
        }
        svc.operations.return_value.get.return_value.execute.return_value = {
            "status": "DONE",
            # targetId intentionally absent
        }
        with pytest.raises(SyncError) as exc_info:
            create_backup(svc, FAST_CFG)
        assert exc_info.value.exit_code == 4

    def test_non_integer_target_id_raises_sync_error(self):
        svc = MagicMock()
        svc.backupRuns.return_value.insert.return_value.execute.return_value = {
            "name": "operations/backup-op"
        }
        svc.operations.return_value.get.return_value.execute.return_value = {
            "status": "DONE",
            "targetId": "not-a-number",
        }
        with pytest.raises(SyncError) as exc_info:
            create_backup(svc, FAST_CFG)
        assert exc_info.value.exit_code == 4

    def test_missing_op_name_raises_sync_error(self):
        svc = MagicMock()
        svc.backupRuns.return_value.insert.return_value.execute.return_value = {}
        with pytest.raises(SyncError) as exc_info:
            create_backup(svc, FAST_CFG)
        assert exc_info.value.exit_code == 4


# ---------------------------------------------------------------------------
# restore_to_target
# ---------------------------------------------------------------------------

class TestRestoreToTarget:

    def test_happy_path(self):
        svc = MagicMock()
        svc.instances.return_value.restoreBackup.return_value.execute.return_value = {
            "name": "operations/restore-op"
        }
        svc.operations.return_value.get.return_value.execute.return_value = {
            "status": "DONE"
        }
        restore_to_target(svc, 42, FAST_TARGET, FAST_CFG)  # should not raise

    def test_http_403_raises_sync_error_with_permissions_hint(self):
        svc = MagicMock()
        svc.instances.return_value.restoreBackup.return_value.execute.side_effect = (
            make_http_error(403)
        )
        with pytest.raises(SyncError) as exc_info:
            restore_to_target(svc, 42, FAST_TARGET, FAST_CFG)
        assert "permissions" in str(exc_info.value).lower()

    def test_http_400_raises_sync_error_with_version_hint(self):
        svc = MagicMock()
        svc.instances.return_value.restoreBackup.return_value.execute.side_effect = (
            make_http_error(400)
        )
        with pytest.raises(SyncError) as exc_info:
            restore_to_target(svc, 42, FAST_TARGET, FAST_CFG)
        assert "mismatch" in str(exc_info.value).lower()

    def test_restore_targets_correct_instance(self):
        """The restore must target the given Target's project/instance and
        reference prod in the backup context."""
        svc = MagicMock()
        svc.instances.return_value.restoreBackup.return_value.execute.return_value = {
            "name": "operations/restore-op"
        }
        svc.operations.return_value.get.return_value.execute.return_value = {
            "status": "DONE"
        }
        target = Target("qa-project", "qa-db")
        restore_to_target(svc, 99, target, FAST_CFG)

        _, kwargs = svc.instances.return_value.restoreBackup.call_args
        # Restore is issued against the target instance...
        assert kwargs["project"] == "qa-project"
        assert kwargs["instance"] == "qa-db"
        # ...but the backup it pulls from points back at prod.
        ctx = kwargs["body"]["restoreBackupContext"]
        assert ctx["project"] == FAST_CFG.prod_project
        assert ctx["instanceId"] == FAST_CFG.prod_instance
        assert ctx["backupRunId"] == 99


# ---------------------------------------------------------------------------
# delete_backup
# ---------------------------------------------------------------------------

class TestDeleteBackup:

    def test_happy_path(self):
        svc = MagicMock()
        svc.backupRuns.return_value.delete.return_value.execute.return_value = {
            "name": "operations/delete-op"
        }
        svc.operations.return_value.get.return_value.execute.return_value = {
            "status": "DONE"
        }
        delete_backup(svc, 42, FAST_CFG)  # should not raise

    def test_sync_error_from_poller_is_non_fatal(self):
        """A SyncError during cleanup must not propagate — just warn."""
        svc = MagicMock()
        svc.backupRuns.return_value.delete.return_value.execute.return_value = {
            "name": "operations/delete-op"
        }
        svc.operations.return_value.get.return_value.execute.side_effect = make_http_error(500)
        delete_backup(svc, 42, FAST_CFG)  # should not raise

    def test_operation_timeout_is_non_fatal(self):
        svc = MagicMock()
        svc.backupRuns.return_value.delete.return_value.execute.return_value = {
            "name": "operations/delete-op"
        }
        # Simulate timeout by making monotonic immediately exceed the deadline
        start = 1000.0
        with patch("main.time.monotonic", side_effect=[start, start + 9999]):
            with patch("main.time.sleep"):
                delete_backup(svc, 42, FAST_CFG)  # should not raise

    def test_missing_op_name_warns_and_returns(self):
        svc = MagicMock()
        svc.backupRuns.return_value.delete.return_value.execute.return_value = {}
        # wait_for_operation should never be called if name is missing
        delete_backup(svc, 42, FAST_CFG)
        svc.operations.assert_not_called()

    def test_unexpected_exception_is_non_fatal(self):
        svc = MagicMock()
        svc.backupRuns.return_value.delete.return_value.execute.side_effect = RuntimeError("boom")
        delete_backup(svc, 42, FAST_CFG)  # should not raise

    def test_http_error_on_delete_call_is_non_fatal(self):
        svc = MagicMock()
        svc.backupRuns.return_value.delete.return_value.execute.side_effect = make_http_error(404)
        delete_backup(svc, 42, FAST_CFG)  # should not raise


# ---------------------------------------------------------------------------
# SyncError / OperationTimeout
# ---------------------------------------------------------------------------

class TestExceptions:

    def test_sync_error_default_exit_code(self):
        err = SyncError("something failed")
        assert err.exit_code == 2

    def test_sync_error_custom_exit_code(self):
        err = SyncError("config bad", exit_code=1)
        assert err.exit_code == 1

    def test_operation_timeout_exit_code(self):
        err = OperationTimeout("operations/abc", 3600)
        assert err.exit_code == 3
        assert "3600" in str(err)
        assert "operations/abc" in str(err)

    def test_operation_timeout_is_subclass_of_sync_error(self):
        err = OperationTimeout("op", 60)
        assert isinstance(err, SyncError)


# ---------------------------------------------------------------------------
# reset_target_password
# ---------------------------------------------------------------------------

class TestResetTargetPassword:

    def _make_service(self):
        svc = MagicMock()
        svc.users.return_value.update.return_value.execute.return_value = {
            "name": "operations/user-update-op"
        }
        svc.operations.return_value.get.return_value.execute.return_value = {
            "status": "DONE"
        }
        return svc

    def test_happy_path_resets_password(self):
        svc = self._make_service()
        reset_target_password(svc, FAST_TARGET, FAST_CFG, "s3cr3t")

        _, kwargs = svc.users.return_value.update.call_args
        assert kwargs["name"] == "postgres"
        assert kwargs["body"]["password"] == "s3cr3t"
        assert kwargs["project"] == FAST_TARGET.project
        assert kwargs["instance"] == FAST_TARGET.instance

    def test_targets_the_given_target(self):
        """Password reset must hit the supplied target, not a fixed instance."""
        svc = self._make_service()
        target = Target("qa-project", "qa-db")
        reset_target_password(svc, target, FAST_CFG, "pw")
        _, kwargs = svc.users.return_value.update.call_args
        assert kwargs["project"] == "qa-project"
        assert kwargs["instance"] == "qa-db"

    def test_http_403_on_user_update_raises_sync_error(self):
        svc = MagicMock()
        svc.users.return_value.update.return_value.execute.side_effect = make_http_error(403)
        with pytest.raises(SyncError) as exc_info:
            reset_target_password(svc, FAST_TARGET, FAST_CFG, "s3cr3t")
        assert exc_info.value.exit_code == 2

    def test_http_404_on_user_update_raises_sync_error(self):
        svc = MagicMock()
        svc.users.return_value.update.return_value.execute.side_effect = make_http_error(404)
        with pytest.raises(SyncError):
            reset_target_password(svc, FAST_TARGET, FAST_CFG, "s3cr3t")

    def test_polls_operation_after_user_update(self):
        """users.update returns an operation — it must be polled to completion."""
        svc = self._make_service()
        reset_target_password(svc, FAST_TARGET, FAST_CFG, "pw")
        svc.operations.return_value.get.assert_called()


class TestFetchSecret:

    def test_calls_secret_manager_client(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.payload.data = b"my-secret-password"
        mock_client.access_secret_version.return_value = mock_response

        with patch("main.secretmanager.SecretManagerServiceClient",
                   return_value=mock_client):
            result = _fetch_secret("projects/p/secrets/s")

        assert result == "my-secret-password"
        mock_client.access_secret_version.assert_called_once_with(
            request={"name": "projects/p/secrets/s/versions/latest"}
        )

    def test_propagates_exception_on_failure(self):
        mock_client = MagicMock()
        mock_client.access_secret_version.side_effect = Exception("not found")

        with patch("main.secretmanager.SecretManagerServiceClient",
                   return_value=mock_client):
            with pytest.raises(Exception, match="not found"):
                _fetch_secret("projects/p/secrets/s")


# ---------------------------------------------------------------------------
# _bool_env
# ---------------------------------------------------------------------------

class TestBoolEnv:

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", "On"])
    def test_truthy_values(self, value):
        with patch.dict("os.environ", {"FLAG": value}, clear=True):
            assert _bool_env("FLAG") is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "nope", "2"])
    def test_falsy_values(self, value):
        with patch.dict("os.environ", {"FLAG": value}, clear=True):
            assert _bool_env("FLAG") is False

    def test_unset_uses_default(self):
        with patch.dict("os.environ", {}, clear=True):
            assert _bool_env("FLAG", default=False) is False
            assert _bool_env("FLAG", default=True) is True

    def test_empty_uses_default(self):
        with patch.dict("os.environ", {"FLAG": "  "}, clear=True):
            assert _bool_env("FLAG", default=True) is True


# ---------------------------------------------------------------------------
# load_config — use_latest_existing_backup
# ---------------------------------------------------------------------------

class TestLoadConfigBackupMode:

    def test_defaults_to_create_new(self):
        with patch.dict("os.environ", VALID_ENV, clear=True):
            cfg = load_config()
        assert cfg.use_latest_existing_backup is False

    def test_enabled_via_env(self):
        env = {**VALID_ENV, "USE_LATEST_EXISTING_BACKUP": "true"}
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.use_latest_existing_backup is True

    def test_explicit_false(self):
        env = {**VALID_ENV, "USE_LATEST_EXISTING_BACKUP": "false"}
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.use_latest_existing_backup is False


# ---------------------------------------------------------------------------
# get_latest_backup
# ---------------------------------------------------------------------------

class TestGetLatestBackup:

    def _service_with_backups(self, items):
        svc = MagicMock()
        svc.backupRuns.return_value.list.return_value.execute.return_value = {"items": items}
        return svc

    def test_picks_most_recent_successful(self):
        # Out of order on purpose — largest id (newest) must win.
        svc = self._service_with_backups([
            {"id": "100", "status": "SUCCESSFUL", "type": "AUTOMATED"},
            {"id": "300", "status": "SUCCESSFUL", "type": "ON_DEMAND"},
            {"id": "200", "status": "SUCCESSFUL", "type": "AUTOMATED"},
        ])
        assert get_latest_backup(svc, FAST_CFG) == 300

    def test_skips_non_successful(self):
        svc = self._service_with_backups([
            {"id": "500", "status": "RUNNING"},
            {"id": "400", "status": "FAILED"},
            {"id": "150", "status": "SUCCESSFUL"},
        ])
        # 500/400 are newer but not SUCCESSFUL → must pick 150.
        assert get_latest_backup(svc, FAST_CFG) == 150

    def test_no_successful_raises_sync_error(self):
        svc = self._service_with_backups([
            {"id": "1", "status": "FAILED"},
            {"id": "2", "status": "RUNNING"},
        ])
        with pytest.raises(SyncError) as exc_info:
            get_latest_backup(svc, FAST_CFG)
        assert "USE_LATEST_EXISTING_BACKUP" in str(exc_info.value)

    def test_empty_list_raises_sync_error(self):
        svc = self._service_with_backups([])
        with pytest.raises(SyncError):
            get_latest_backup(svc, FAST_CFG)

    def test_http_error_raises_sync_error(self):
        svc = MagicMock()
        svc.backupRuns.return_value.list.return_value.execute.side_effect = make_http_error(403)
        with pytest.raises(SyncError) as exc_info:
            get_latest_backup(svc, FAST_CFG)
        assert exc_info.value.exit_code == 2

    def test_does_not_create_a_backup(self):
        svc = self._service_with_backups([{"id": "9", "status": "SUCCESSFUL"}])
        get_latest_backup(svc, FAST_CFG)
        svc.backupRuns.return_value.insert.assert_not_called()


# ---------------------------------------------------------------------------
# acquire_backup — ownership decision
# ---------------------------------------------------------------------------

class TestAcquireBackup:

    def test_create_new_when_disabled(self):
        cfg = Config(**{**FAST_CFG.__dict__, "use_latest_existing_backup": False})
        svc = MagicMock()
        svc.backupRuns.return_value.insert.return_value.execute.return_value = {
            "name": "operations/backup-op"
        }
        svc.operations.return_value.get.return_value.execute.return_value = {
            "status": "DONE", "targetId": "777",
        }
        backup_id, owned = acquire_backup(svc, cfg)
        assert backup_id == 777
        assert owned is True

    def test_reuse_existing_when_enabled(self):
        cfg = Config(**{**FAST_CFG.__dict__, "use_latest_existing_backup": True})
        svc = MagicMock()
        svc.backupRuns.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "888", "status": "SUCCESSFUL"}]
        }
        backup_id, owned = acquire_backup(svc, cfg)
        assert backup_id == 888
        assert owned is False
        # Must NOT create a new backup when reusing.
        svc.backupRuns.return_value.insert.assert_not_called()


# ---------------------------------------------------------------------------
# Public-egress runtime warning
# ---------------------------------------------------------------------------

class TestPublicNetworkWarning:
    """main() must warn when the job's egress is public (SYNC_NETWORK_MODE
    unset or != 'private'), and stay quiet when private networking is set."""

    WARNING_FRAGMENT = "egress is PUBLIC"

    def _run_main(self, extra_env, caplog):
        env = {**VALID_ENV, **extra_env}
        with patch.dict("os.environ", env, clear=True), \
             patch("main.build_sqladmin", return_value=MagicMock()), \
             patch("main.acquire_backup", return_value=(1, True)), \
             patch("main.restore_to_target"), \
             patch("main.verify_target"), \
             patch("main.delete_backup"):
            with caplog.at_level(logging.WARNING):
                main.main()
        return caplog.text

    def test_warns_when_mode_unset(self, caplog):
        assert self.WARNING_FRAGMENT in self._run_main({}, caplog)

    def test_warns_when_mode_public(self, caplog):
        assert self.WARNING_FRAGMENT in self._run_main(
            {"SYNC_NETWORK_MODE": "public"}, caplog)

    def test_silent_when_mode_private(self, caplog):
        assert self.WARNING_FRAGMENT not in self._run_main(
            {"SYNC_NETWORK_MODE": "private"}, caplog)

    def test_private_is_case_insensitive(self, caplog):
        assert self.WARNING_FRAGMENT not in self._run_main(
            {"SYNC_NETWORK_MODE": " Private "}, caplog)


# ---------------------------------------------------------------------------
# verify_target — post-restore verification
# ---------------------------------------------------------------------------

class TestVerifyTarget:

    def _service(self, state="RUNNABLE", connection_name="p:r:i"):
        svc = MagicMock()
        svc.instances.return_value.get.return_value.execute.return_value = {
            "state": state,
            "connectionName": connection_name,
        }
        return svc

    def test_api_only_when_no_password(self):
        """Without a password, only the API-level RUNNABLE check runs."""
        svc = self._service()
        with patch("main._verify_sql") as sql:
            main.verify_target(svc, FAST_TARGET, FAST_CFG, password=None)
        sql.assert_not_called()

    def test_not_runnable_raises(self):
        svc = self._service(state="MAINTENANCE")
        with pytest.raises(SyncError) as exc_info:
            main.verify_target(svc, FAST_TARGET, FAST_CFG, password=None)
        assert "MAINTENANCE" in str(exc_info.value)

    def test_sql_check_runs_with_password(self):
        svc = self._service(connection_name="proj:region:inst")
        with patch("main._verify_sql") as sql:
            main.verify_target(svc, FAST_TARGET, FAST_CFG, password="pw")
        sql.assert_called_once_with("proj:region:inst", "pw")

    def test_sql_failure_raises_sync_error(self):
        svc = self._service()
        with patch("main._verify_sql", side_effect=RuntimeError("connection refused")):
            with pytest.raises(SyncError) as exc_info:
                main.verify_target(svc, FAST_TARGET, FAST_CFG, password="pw")
        assert "connection refused" in str(exc_info.value)

    def test_missing_connection_name_falls_back_to_region(self):
        svc = MagicMock()
        svc.instances.return_value.get.return_value.execute.return_value = {
            "state": "RUNNABLE",
        }
        with patch("main._verify_sql") as sql:
            main.verify_target(svc, FAST_TARGET, FAST_CFG, password="pw")
        expected = f"{FAST_TARGET.project}:{FAST_CFG.region}:{FAST_TARGET.instance}"
        sql.assert_called_once_with(expected, "pw")

    def test_api_error_raises_sync_error(self):
        svc = MagicMock()
        svc.instances.return_value.get.return_value.execute.side_effect = make_http_error(403)
        with pytest.raises(SyncError):
            main.verify_target(svc, FAST_TARGET, FAST_CFG, password=None)


class TestVerifyWiredIntoMain:

    def _run(self, extra_env):
        env = {**VALID_ENV, **extra_env}
        calls = {}
        with patch.dict("os.environ", env, clear=True), \
             patch("main.build_sqladmin", return_value=MagicMock()), \
             patch("main.acquire_backup", return_value=(1, True)), \
             patch("main.restore_to_target"), \
             patch("main.delete_backup"), \
             patch("main.verify_target") as verify:
            main.main()
            calls["verify"] = verify
        return calls["verify"]

    def test_verification_on_by_default(self):
        verify = self._run({})
        verify.assert_called_once()

    def test_verification_can_be_disabled(self):
        verify = self._run({"VERIFY_RESTORE": "false"})
        verify.assert_not_called()
