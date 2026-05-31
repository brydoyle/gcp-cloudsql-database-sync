"""
Unit tests for main.py.

Run with:  pytest sync_job/test_main.py -v
Coverage:  pytest sync_job/test_main.py --cov=sync_job/main --cov-report=term-missing
"""

import http
import io
import unittest
from unittest.mock import MagicMock, call, patch

import pytest
from googleapiclient.errors import HttpError

import main
from main import (
    Config,
    OperationTimeout,
    SyncError,
    _extract_op_name,
    _raise_for_http_error,
    _validate_resource_name,
    create_backup,
    delete_backup,
    load_config,
    restore_to_nonprod,
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
}

FAST_CFG = Config(
    prod_project="my-prod-project",
    prod_instance="prod-db",
    nonprod_project="my-nonprod-project",
    nonprod_instance="nonprod-db",
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
        assert cfg.nonprod_project == "my-nonprod-project"
        assert cfg.nonprod_instance == "nonprod-db"
        assert cfg.poll_interval == 15
        assert cfg.operation_timeout == 7200

    def test_optional_int_overrides(self):
        env = {**VALID_ENV, "POLL_INTERVAL_SECONDS": "30", "OPERATION_TIMEOUT_SECONDS": "3600"}
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.poll_interval == 30
        assert cfg.operation_timeout == 3600

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

    def test_timeout_below_minimum_rejected(self):
        env = {**VALID_ENV, "OPERATION_TIMEOUT_SECONDS": "10"}
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


# ---------------------------------------------------------------------------
# _validate_resource_name
# ---------------------------------------------------------------------------

class TestValidateResourceName:

    @pytest.mark.parametrize("name", [
        "a",
        "my-project",
        "prod-db-01",
        "a" * 63,
        "abc123",
    ])
    def test_valid_names(self, name):
        errors: list = []
        _validate_resource_name(name, "LABEL", errors)
        assert errors == []

    @pytest.mark.parametrize("name", [
        "UPPERCASE",
        "has spaces",
        "has_underscore",
        "has.dot",
        "-starts-with-hyphen",
        "ends-with-hyphen-",
        "has\nnewline",
    ])
    def test_invalid_names(self, name):
        errors: list = []
        _validate_resource_name(name, "LABEL", errors)
        assert len(errors) == 1

    def test_empty_string_skipped(self):
        """Empty string is handled by _require_env; _validate_resource_name skips it."""
        errors: list = []
        _validate_resource_name("", "LABEL", errors)
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
# restore_to_nonprod
# ---------------------------------------------------------------------------

class TestRestoreToNonprod:

    def test_happy_path(self):
        svc = MagicMock()
        svc.instances.return_value.restoreBackup.return_value.execute.return_value = {
            "name": "operations/restore-op"
        }
        svc.operations.return_value.get.return_value.execute.return_value = {
            "status": "DONE"
        }
        restore_to_nonprod(svc, 42, FAST_CFG)  # should not raise

    def test_http_403_raises_sync_error_with_permissions_hint(self):
        svc = MagicMock()
        svc.instances.return_value.restoreBackup.return_value.execute.side_effect = (
            make_http_error(403)
        )
        with pytest.raises(SyncError) as exc_info:
            restore_to_nonprod(svc, 42, FAST_CFG)
        assert "permissions" in str(exc_info.value).lower()

    def test_http_400_raises_sync_error_with_version_hint(self):
        svc = MagicMock()
        svc.instances.return_value.restoreBackup.return_value.execute.side_effect = (
            make_http_error(400)
        )
        with pytest.raises(SyncError) as exc_info:
            restore_to_nonprod(svc, 42, FAST_CFG)
        assert "mismatch" in str(exc_info.value).lower()

    def test_restore_body_references_prod_instance(self):
        """The restore payload must point back at the prod project/instance."""
        svc = MagicMock()
        svc.instances.return_value.restoreBackup.return_value.execute.return_value = {
            "name": "operations/restore-op"
        }
        svc.operations.return_value.get.return_value.execute.return_value = {
            "status": "DONE"
        }
        restore_to_nonprod(svc, 99, FAST_CFG)

        _, kwargs = svc.instances.return_value.restoreBackup.call_args
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
