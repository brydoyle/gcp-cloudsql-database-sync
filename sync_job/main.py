"""
Cloud Run Job: sync a production CloudSQL PostgreSQL instance to a non-production
instance in a different GCP project using native Cloud SQL backup snapshots.

Flow:
  1. Validate config (including prod ≠ nonprod guard)
  2. Create an on-demand backup of the prod instance
  3. Wait for the backup to finish
  4. Restore that backup to the non-prod instance (cross-project)
  5. Wait for the restore to finish
  6. Delete the on-demand backup to avoid quota accumulation

No GCS bucket or dump files involved — entirely managed by Cloud SQL.

Required env vars: see Config section below.
IAM requirements: see README.md.

Exit codes:
  0 — success
  1 — configuration error (missing/invalid env vars)
  2 — Cloud SQL API error
  3 — operation timed out
  4 — unexpected error
"""

import dataclasses
import logging
import os
import re
import sys
import time

import googleapiclient.discovery
from google.auth import default
from google.auth.exceptions import DefaultCredentialsError
from googleapiclient.errors import HttpError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# GCP project IDs: 6–30 chars, must start with a lowercase letter,
# lowercase letters/digits/hyphens only, no trailing hyphen.
# https://cloud.google.com/resource-manager/docs/creating-managing-projects
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9\-]{4,28}[a-z0-9]$")

# Cloud SQL instance names: 1–98 chars, must start with a lowercase letter,
# lowercase letters/digits/hyphens only, no trailing hyphen.
# https://cloud.google.com/sql/docs/postgres/instance-settings
_INSTANCE_NAME_RE = re.compile(r"^[a-z]([a-z0-9\-]{0,96}[a-z0-9])?$")

# Valid GCP regions for Cloud SQL / Cloud Run.
# https://cloud.google.com/about/locations
_GCP_REGIONS: frozenset[str] = frozenset({
    # Americas
    "us-central1", "us-east1", "us-east4", "us-east5", "us-south1",
    "us-west1", "us-west2", "us-west3", "us-west4",
    "northamerica-northeast1", "northamerica-northeast2",
    "southamerica-east1", "southamerica-west1",
    # Europe
    "europe-central2", "europe-north1", "europe-southwest1",
    "europe-west1", "europe-west2", "europe-west3", "europe-west4",
    "europe-west6", "europe-west8", "europe-west9", "europe-west10", "europe-west12",
    # Asia Pacific
    "asia-east1", "asia-east2",
    "asia-northeast1", "asia-northeast2", "asia-northeast3",
    "asia-south1", "asia-south2",
    "asia-southeast1", "asia-southeast2",
    "australia-southeast1", "australia-southeast2",
    # Middle East & Africa
    "me-central1", "me-central2", "me-west1",
    "africa-south1",
})


# ---------------------------------------------------------------------------
# Typed exceptions
# wait_for_operation raises these instead of calling sys.exit() so callers
# (including non-fatal delete_backup) can handle failures at the right level.
# ---------------------------------------------------------------------------

class SyncError(Exception):
    """Fatal sync error. Caught at the top level and converted to sys.exit."""
    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


class OperationTimeout(SyncError):
    def __init__(self, operation_name: str, timeout: int):
        super().__init__(
            f"Operation {operation_name} did not complete within {timeout}s",
            exit_code=3,
        )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Config:
    prod_project: str
    prod_instance: str
    nonprod_project: str
    nonprod_instance: str
    region: str
    poll_interval: int
    operation_timeout: int


def _require_env(name: str, errors: list) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        errors.append(f"Missing required environment variable: {name}")
    return value


def _optional_int_env(
    name: str, default_value: int, minimum: int, maximum: int, errors: list
) -> int:
    raw = os.getenv(name, str(default_value))
    try:
        value = int(raw)
    except ValueError:
        errors.append(
            f"Environment variable {name} must be an integer, got: {raw!r}"
        )
        return default_value
    if value < minimum or value > maximum:
        errors.append(
            f"Environment variable {name} must be between {minimum} and {maximum}, got {value}"
        )
        return default_value
    return value


def _validate_project_id(value: str, label: str, errors: list) -> None:
    """GCP project IDs: 6–30 chars, starts with a letter, letters/digits/hyphens,
    no trailing hyphen."""
    if not value:
        return  # already caught by _require_env
    if not _PROJECT_ID_RE.match(value):
        errors.append(
            f"{label} {value!r} is not a valid GCP project ID "
            "(6–30 chars, must start with a lowercase letter, "
            "lowercase letters/digits/hyphens only, no trailing hyphen)"
        )


def _validate_instance_name(value: str, label: str, errors: list) -> None:
    """Cloud SQL instance names: 1–98 chars, starts with a letter,
    letters/digits/hyphens, no trailing hyphen."""
    if not value:
        return
    if not _INSTANCE_NAME_RE.match(value):
        errors.append(
            f"{label} {value!r} is not a valid Cloud SQL instance name "
            "(1–98 chars, must start with a lowercase letter, "
            "lowercase letters/digits/hyphens only, no trailing hyphen)"
        )


def _validate_region(value: str, label: str, errors: list) -> None:
    """Validate against the known set of GCP regions."""
    if not value:
        return
    if value not in _GCP_REGIONS:
        errors.append(
            f"{label} {value!r} is not a recognised GCP region. "
            f"Valid examples: us-central1, europe-west1, asia-east1. "
            f"Full list: https://cloud.google.com/about/locations"
        )


def load_config() -> Config:
    """Validate and return config. Collects ALL errors before exiting so a
    misconfigured environment reports every problem in a single run."""
    errors: list[str] = []

    prod_project     = _require_env("PROD_PROJECT_ID",      errors)
    prod_instance    = _require_env("PROD_INSTANCE_NAME",   errors)
    nonprod_project  = _require_env("NONPROD_PROJECT_ID",   errors)
    nonprod_instance = _require_env("NONPROD_INSTANCE_NAME", errors)
    region           = _require_env("GCP_REGION",           errors)

    # POLL_INTERVAL_SECONDS: 1–3600s (1 second to 1 hour)
    poll = _optional_int_env(
        "POLL_INTERVAL_SECONDS", default_value=15, minimum=1, maximum=3600, errors=errors
    )
    # OPERATION_TIMEOUT_SECONDS: 60s–86400s (1 minute to 24 hours)
    timeout = _optional_int_env(
        "OPERATION_TIMEOUT_SECONDS", default_value=7200, minimum=60, maximum=86400, errors=errors
    )

    _validate_project_id(prod_project,     "PROD_PROJECT_ID",      errors)
    _validate_project_id(nonprod_project,  "NONPROD_PROJECT_ID",   errors)
    _validate_instance_name(prod_instance,    "PROD_INSTANCE_NAME",   errors)
    _validate_instance_name(nonprod_instance, "NONPROD_INSTANCE_NAME", errors)
    _validate_region(region, "GCP_REGION", errors)

    if errors:
        for err in errors:
            log.error(err)
        sys.exit(1)

    # Safety guard: prevent restoring prod onto itself.
    if prod_project == nonprod_project and prod_instance == nonprod_instance:
        log.error(
            "PROD and NONPROD point to the same instance (%s/%s). "
            "Refusing to continue to avoid overwriting production data.",
            prod_project, prod_instance,
        )
        sys.exit(1)

    return Config(
        prod_project=prod_project,
        prod_instance=prod_instance,
        nonprod_project=nonprod_project,
        nonprod_instance=nonprod_instance,
        region=region,
        poll_interval=poll,
        operation_timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_sqladmin():
    """Build and return an authenticated Cloud SQL Admin API client."""
    try:
        credentials, project = default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    except DefaultCredentialsError as exc:
        log.error("Could not obtain GCP credentials: %s", exc)
        sys.exit(1)

    log.info("Authenticated (default project: %s)", project or "n/a")
    # cache_discovery=True (the default) caches the discovery doc on disk so
    # repeated runs skip the extra HTTP round-trip.
    return googleapiclient.discovery.build(
        "sqladmin", "v1beta4", credentials=credentials
    )


def _extract_op_name(op: dict, context: str) -> str:
    """Return the operation name, raising SyncError if it is absent."""
    name = op.get("name")
    if not name:
        raise SyncError(
            f"API response for {context} is missing 'name' field: {op}",
            exit_code=4,
        )
    return name


_HTTP_HINTS: dict[int, str] = {
    400: "bad request — possible causes: (1) GCP Free Trial instance — upgrade to a paid account; (2) instance tier or PostgreSQL version mismatch between prod and non-prod",
    403: "check that the job service account has the required cloudsql.* permissions",
    404: "instance or backup not found — verify instance names and backup ID",
    409: "a conflicting operation (backup, restore, or maintenance) is already in progress",
}


def _raise_for_http_error(exc: HttpError, context: str) -> None:
    """Convert an HttpError into a SyncError with an actionable hint."""
    hint = _HTTP_HINTS.get(exc.status_code, "")
    msg = f"HTTP {exc.status_code} {context}: {exc.reason}"
    if hint:
        msg += f"\n  → {hint}"
    raise SyncError(msg) from exc


def wait_for_operation(service, project: str, operation_name: str, cfg: Config) -> dict:
    """Poll until the Cloud SQL operation reaches a terminal state.

    Raises SyncError or OperationTimeout — never calls sys.exit() — so
    non-fatal callers (delete_backup) can catch and handle failures cleanly.
    execute(num_retries=10) handles transient transport errors internally.
    """
    deadline = time.monotonic() + cfg.operation_timeout

    while True:
        if time.monotonic() >= deadline:
            raise OperationTimeout(operation_name, cfg.operation_timeout)

        try:
            result = (
                service.operations()
                .get(project=project, operation=operation_name)
                .execute(num_retries=10)
            )
        except HttpError as exc:
            _raise_for_http_error(exc, f"polling operation {operation_name}")

        status = result.get("status")
        log.info("  operation %s → %s", operation_name, status)

        if status == "DONE":
            if "error" in result:
                errors = result["error"].get("errors", [result["error"]])
                messages = "; ".join(
                    f"[{e.get('code', 'UNKNOWN')}] {e.get('message', 'no message')}"
                    for e in errors
                )
                raise SyncError(f"Operation {operation_name} failed: {messages}")
            return result

        # Adaptive sleep: don't overshoot the deadline on the last interval.
        remaining = deadline - time.monotonic()
        time.sleep(min(cfg.poll_interval, max(remaining, 0)))


# ---------------------------------------------------------------------------
# Core steps
# ---------------------------------------------------------------------------

def create_backup(service, cfg: Config) -> int:
    """Trigger an on-demand backup of the prod instance and return its ID."""
    log.info("Creating on-demand backup of %s/%s ...", cfg.prod_project, cfg.prod_instance)
    try:
        op = (
            service.backupRuns()
            .insert(project=cfg.prod_project, instance=cfg.prod_instance, body={})
            .execute(num_retries=5)
        )
    except HttpError as exc:
        _raise_for_http_error(exc, f"starting backup on {cfg.prod_project}/{cfg.prod_instance}")

    op_name = _extract_op_name(op, "backupRuns.insert")
    log.info("Backup operation started: %s", op_name)
    result = wait_for_operation(service, cfg.prod_project, op_name, cfg)

    # The Cloud SQL API returns the backup ID in backupContext.backupId.
    # Older API versions used targetId — check both for compatibility.
    raw_id = (
        (result.get("backupContext") or {}).get("backupId")
        or result.get("targetId")
    )
    try:
        backup_id = int(raw_id or "")
    except (ValueError, TypeError) as exc:
        raise SyncError(
            f"Operation {op_name} completed but backup ID is missing or non-integer: {result}",
            exit_code=4,
        ) from exc

    log.info("Backup complete. Run ID: %d", backup_id)
    return backup_id


def restore_to_nonprod(service, backup_id: int, cfg: Config) -> None:
    """Restore the prod backup to the non-prod instance (cross-project)."""
    log.info(
        "Restoring backup %d → %s/%s ...",
        backup_id, cfg.nonprod_project, cfg.nonprod_instance,
    )
    body = {
        "restoreBackupContext": {
            "kind": "sql#restoreBackupContext",
            "backupRunId": backup_id,
            "instanceId": cfg.prod_instance,
            "project": cfg.prod_project,
        }
    }
    try:
        op = (
            service.instances()
            .restoreBackup(
                project=cfg.nonprod_project,
                instance=cfg.nonprod_instance,
                body=body,
            )
            .execute(num_retries=5)
        )
    except HttpError as exc:
        _raise_for_http_error(
            exc,
            f"starting restore on {cfg.nonprod_project}/{cfg.nonprod_instance}",
        )

    op_name = _extract_op_name(op, "instances.restoreBackup")
    log.info("Restore operation started: %s", op_name)
    wait_for_operation(service, cfg.nonprod_project, op_name, cfg)
    log.info("Restore complete.")


def delete_backup(service, backup_id: int, cfg: Config) -> None:
    """Delete the on-demand backup to avoid accumulating against quota.

    Entirely non-fatal: catches SyncError/OperationTimeout from
    wait_for_operation so cleanup never masks a successful sync.
    """
    log.info(
        "Deleting on-demand backup %d from %s/%s ...",
        backup_id, cfg.prod_project, cfg.prod_instance,
    )
    try:
        op = (
            service.backupRuns()
            .delete(
                project=cfg.prod_project,
                instance=cfg.prod_instance,
                id=backup_id,
            )
            .execute(num_retries=5)
        )
        op_name = op.get("name")
        if not op_name:
            log.warning(
                "Delete response for backup %d missing 'name'; cannot poll — "
                "backup may need to be removed manually.",
                backup_id,
            )
            return
        log.info("Backup delete operation started: %s", op_name)
        wait_for_operation(service, cfg.prod_project, op_name, cfg)
        log.info("Backup %d deleted.", backup_id)
    except (SyncError, OperationTimeout) as exc:
        log.warning(
            "Backup %d cleanup failed (%s) — remove it manually from %s/%s.",
            backup_id, exc, cfg.prod_project, cfg.prod_instance,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Unexpected error deleting backup %d: %s — remove it manually.",
            backup_id, exc,
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()
    log.info(
        "Starting sync: %s/%s → %s/%s  (region: %s)",
        cfg.prod_project, cfg.prod_instance,
        cfg.nonprod_project, cfg.nonprod_instance,
        cfg.region,
    )
    log.info(
        "Config: poll_interval=%ds  timeout=%ds",
        cfg.poll_interval, cfg.operation_timeout,
    )

    service = build_sqladmin()
    backup_id = create_backup(service, cfg)

    try:
        restore_to_nonprod(service, backup_id, cfg)
    finally:
        # Always attempt cleanup so backups don't accumulate even on failure.
        delete_backup(service, backup_id, cfg)

    log.info("Sync finished successfully.")


if __name__ == "__main__":
    try:
        main()
    except SyncError as exc:
        log.error("%s", exc)
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        sys.exit(4)
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error: %s", exc)
        sys.exit(4)
