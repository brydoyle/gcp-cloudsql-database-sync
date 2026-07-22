"""
Cloud Run Job: sync a production CloudSQL PostgreSQL instance to a non-production
instance in a different GCP project using native Cloud SQL backup snapshots.

Flow:
  1. Validate config (including prod ≠ nonprod guard)
  2. Acquire a backup of the prod instance — either create a fresh on-demand
     backup, or (USE_LATEST_EXISTING_BACKUP) reuse the newest existing one
  3. Wait for the backup to finish (only when creating)
  4. Restore that backup to each non-prod target (cross-project)
  5. Wait for each restore to finish
  6. Delete the backup to avoid quota accumulation — ONLY if this job
     created it; a reused pre-existing backup is left in place
  7. (Optional) Reset each target's postgres password from Secret Manager

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
import json
import logging
import os
import re
import secrets
import sys
import time

import googleapiclient.discovery
from google.auth import default
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import secretmanager
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
class Target:
    """A single non-production restore target."""
    project: str
    instance: str

    def __str__(self) -> str:
        return f"{self.project}/{self.instance}"


@dataclasses.dataclass(frozen=True)
class Config:
    prod_project: str
    prod_instance: str
    nonprod_targets: tuple  # tuple[Target, ...]
    region: str
    poll_interval: int
    operation_timeout: int
    use_latest_existing_backup: bool = False
    permission_mappings: tuple = ()   # ({"from": principal, "to": principal}, ...)
    permission_grants: tuple = ()     # ({"identity": principal, "roles": [...]}, ...)
    revoke_source_login: bool = True
    permission_admin_mode: str = "auto"  # auto | ephemeral | postgres


# Cloud SQL IAM principals → PostgreSQL role names. Service-account roles drop
# the ".gserviceaccount.com" suffix; human users keep the full email.
_IAM_SA_SUFFIX = ".gserviceaccount.com"

# Conservative identifier charset for anything we interpolate into SQL.
_SQL_IDENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_@.\-]*$")


def _iam_role_name(principal: str) -> str:
    """PostgreSQL role name for a Cloud SQL IAM principal."""
    p = principal.strip()
    return p[: -len(_IAM_SA_SUFFIX)] if p.endswith(_IAM_SA_SUFFIX) else p


def _quote_ident(name: str) -> str:
    """Validate then double-quote a SQL identifier. Values come from operator
    config, but we still refuse anything outside a safe charset."""
    if not _SQL_IDENT_RE.match(name or ""):
        raise SyncError(f"Unsafe SQL identifier in permission config: {name!r}", exit_code=1)
    return '"' + name.replace('"', '""') + '"'


def _json_list_env(name: str, errors: list) -> tuple:
    """Parse a JSON-array env var into a tuple of dicts (empty when unset)."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        errors.append(f"{name} must be valid JSON: {exc}")
        return ()
    if not isinstance(value, list) or not all(isinstance(i, dict) for i in value):
        errors.append(f"{name} must be a JSON array of objects")
        return ()
    return tuple(value)


# Recognisable name so a leftover from a crashed run is obvious and greppable.
_EPHEMERAL_ADMIN_USER = "cloudsql-sync-tmp-admin"
_ADMIN_MODES = ("auto", "ephemeral", "postgres")


def _validate_admin_mode(mode: str, errors: list) -> None:
    if mode not in _ADMIN_MODES:
        errors.append(
            f"PERMISSION_ADMIN_MODE must be one of {', '.join(_ADMIN_MODES)}, got {mode!r}"
        )


def _validate_permissions(mappings: tuple, grants: tuple, errors: list) -> None:
    for i, m in enumerate(mappings, 1):
        if not m.get("from") or not m.get("to"):
            errors.append(f"PERMISSION_MAPPINGS[{i}] needs both 'from' and 'to'")
    for i, g in enumerate(grants, 1):
        if not g.get("identity"):
            errors.append(f"PERMISSION_GRANTS[{i}] needs 'identity'")
        roles = g.get("roles")
        if not isinstance(roles, list) or not roles:
            errors.append(f"PERMISSION_GRANTS[{i}] needs a non-empty 'roles' list")


def _require_env(name: str, errors: list) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        errors.append(f"Missing required environment variable: {name}")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    """Parse a boolean env var leniently. Accepts 1/true/yes/on (any case)."""
    raw = os.getenv(name, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


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


def _parse_targets(errors: list) -> tuple:
    """Build the list of non-production restore targets.

    Two input forms, in precedence order:
      1. NONPROD_TARGETS — comma-separated "project:instance" pairs, e.g.
         "dev-proj:dev-db,qa-proj:qa-db". Enables multi-target fan-out.
      2. NONPROD_PROJECT_ID + NONPROD_INSTANCE_NAME — the single-target form
         (backward compatible).

    Each target's project and instance are validated. Returns a tuple of
    Target. On any error, appends to `errors` and returns whatever parsed.
    """
    raw_targets = os.environ.get("NONPROD_TARGETS", "").strip()

    targets: list = []
    if raw_targets:
        for i, entry in enumerate(raw_targets.split(",")):
            entry = entry.strip()
            if not entry:
                continue
            if entry.count(":") != 1:
                errors.append(
                    f"NONPROD_TARGETS entry #{i + 1} {entry!r} must be "
                    "'project:instance'"
                )
                continue
            project, instance = (p.strip() for p in entry.split(":"))
            _validate_project_id(project, f"NONPROD_TARGETS[{i + 1}] project", errors)
            _validate_instance_name(instance, f"NONPROD_TARGETS[{i + 1}] instance", errors)
            targets.append(Target(project=project, instance=instance))
        if not targets:
            errors.append("NONPROD_TARGETS is set but contains no valid targets")
    else:
        # Single-target fallback.
        project  = _require_env("NONPROD_PROJECT_ID",   errors)
        instance = _require_env("NONPROD_INSTANCE_NAME", errors)
        _validate_project_id(project, "NONPROD_PROJECT_ID", errors)
        _validate_instance_name(instance, "NONPROD_INSTANCE_NAME", errors)
        if project and instance:
            targets.append(Target(project=project, instance=instance))

    # Guard against the same target appearing twice — a duplicate restore
    # is wasteful and almost always a config mistake.
    seen = set()
    for t in targets:
        if t in seen:
            errors.append(f"Duplicate restore target: {t}")
        seen.add(t)

    return tuple(targets)


def load_config() -> Config:
    """Validate and return config. Collects ALL errors before exiting so a
    misconfigured environment reports every problem in a single run."""
    errors: list[str] = []

    prod_project  = _require_env("PROD_PROJECT_ID",    errors)
    prod_instance = _require_env("PROD_INSTANCE_NAME", errors)
    region        = _require_env("GCP_REGION",         errors)

    nonprod_targets = _parse_targets(errors)

    # POLL_INTERVAL_SECONDS: 1–3600s (1 second to 1 hour)
    poll = _optional_int_env(
        "POLL_INTERVAL_SECONDS", default_value=15, minimum=1, maximum=3600, errors=errors
    )
    # OPERATION_TIMEOUT_SECONDS: 60s–86400s (1 minute to 24 hours)
    timeout = _optional_int_env(
        "OPERATION_TIMEOUT_SECONDS", default_value=7200, minimum=60, maximum=86400, errors=errors
    )

    perm_mappings = _json_list_env("PERMISSION_MAPPINGS", errors)
    perm_grants   = _json_list_env("PERMISSION_GRANTS", errors)
    _validate_permissions(perm_mappings, perm_grants, errors)
    admin_mode = (os.getenv("PERMISSION_ADMIN_MODE", "auto").strip().lower() or "auto")
    _validate_admin_mode(admin_mode, errors)

    _validate_project_id(prod_project, "PROD_PROJECT_ID", errors)
    _validate_instance_name(prod_instance, "PROD_INSTANCE_NAME", errors)
    _validate_region(region, "GCP_REGION", errors)

    # Safety guard: prevent restoring prod onto itself (checked per target).
    for t in nonprod_targets:
        if t.project == prod_project and t.instance == prod_instance:
            errors.append(
                f"Target {t} is the same as PROD — refusing to continue to "
                "avoid overwriting production data."
            )

    if errors:
        for err in errors:
            log.error(err)
        sys.exit(1)

    return Config(
        prod_project=prod_project,
        prod_instance=prod_instance,
        nonprod_targets=nonprod_targets,
        region=region,
        poll_interval=poll,
        operation_timeout=timeout,
        use_latest_existing_backup=_bool_env("USE_LATEST_EXISTING_BACKUP", default=False),
        permission_mappings=perm_mappings,
        permission_grants=perm_grants,
        revoke_source_login=_bool_env("REVOKE_SOURCE_LOGIN", default=True),
        permission_admin_mode=admin_mode,
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


def get_latest_backup(service, cfg: Config) -> int:
    """Return the ID of the most recent SUCCESSFUL backup of the prod instance.

    Used when USE_LATEST_EXISTING_BACKUP is enabled — reuses an existing
    snapshot instead of creating a new one. Raises SyncError if none exists.
    Backup IDs are epoch-millis integers, so the largest id is the newest;
    we sort explicitly rather than trusting API ordering.
    """
    log.info(
        "Looking up most recent successful backup of %s/%s ...",
        cfg.prod_project, cfg.prod_instance,
    )
    try:
        resp = (
            service.backupRuns()
            .list(project=cfg.prod_project, instance=cfg.prod_instance, maxResults=100)
            .execute(num_retries=5)
        )
    except HttpError as exc:
        _raise_for_http_error(
            exc, f"listing backups on {cfg.prod_project}/{cfg.prod_instance}"
        )

    successful = [
        item for item in resp.get("items", [])
        if item.get("status") == "SUCCESSFUL" and item.get("id")
    ]
    if not successful:
        raise SyncError(
            f"No successful backup found for {cfg.prod_project}/{cfg.prod_instance}. "
            "Create one first, or disable USE_LATEST_EXISTING_BACKUP to create a "
            "fresh backup on each run."
        )

    latest = max(successful, key=lambda item: int(item["id"]))
    backup_id = int(latest["id"])
    log.info(
        "Using existing backup %d (type=%s, ended=%s) — it will NOT be deleted.",
        backup_id, latest.get("type", "?"), latest.get("endTime", "?"),
    )
    return backup_id


def acquire_backup(service, cfg: Config) -> tuple:
    """Obtain a backup to restore from. Returns (backup_id, created_by_us).

    - USE_LATEST_EXISTING_BACKUP on  → reuse newest existing backup, owned=False
    - off (default)                  → create a fresh on-demand backup, owned=True

    The ownership flag tells cleanup whether it may delete the backup: we only
    ever delete backups this job created, never a pre-existing one.
    """
    if cfg.use_latest_existing_backup:
        return get_latest_backup(service, cfg), False
    return create_backup(service, cfg), True


def restore_to_target(service, backup_id: int, target: Target, cfg: Config) -> None:
    """Restore the prod backup to one non-prod target (cross-project)."""
    log.info("Restoring backup %d → %s ...", backup_id, target)
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
                project=target.project,
                instance=target.instance,
                body=body,
            )
            .execute(num_retries=5)
        )
    except HttpError as exc:
        _raise_for_http_error(exc, f"starting restore on {target}")

    op_name = _extract_op_name(op, "instances.restoreBackup")
    log.info("Restore operation started: %s", op_name)
    wait_for_operation(service, target.project, op_name, cfg)
    log.info("Restore to %s complete.", target)


def _verify_sql(connection_name: str, password: str) -> None:
    """Connect to the instance via the Cloud SQL Python Connector and run
    SELECT 1. Proves the engine is up, accepting connections, and that the
    postgres password (reset from Secret Manager) actually works."""
    # Lazy import: the connector is only needed when SQL verification runs,
    # and unit tests patch this function entirely.
    from google.cloud.sql.connector import Connector

    with Connector() as connector:
        conn = connector.connect(
            connection_name, "pg8000",
            user="postgres", password=password, db="postgres",
        )
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            row = cur.fetchone()
            if not row or row[0] != 1:
                raise RuntimeError(f"unexpected SELECT 1 result: {row!r}")
        finally:
            conn.close()


def verify_target(service, target: Target, cfg: Config, password=None) -> None:
    """Post-restore verification for one target.

    Tier 1 (always): instances.get → state must be RUNNABLE. No credentials
    needed beyond the job SA's existing API access.
    Tier 2 (when a password is available, i.e. Secret Manager reset is on):
    open a real SQL connection and SELECT 1 — turns "the API said DONE" into
    "the database actually serves queries with the expected credential".
    """
    log.info("Verifying restore on %s ...", target)
    try:
        inst = (
            service.instances()
            .get(project=target.project, instance=target.instance)
            .execute(num_retries=3)
        )
    except HttpError as exc:
        _raise_for_http_error(exc, f"verifying {target}")

    state = inst.get("state")
    if state != "RUNNABLE":
        raise SyncError(
            f"Post-restore verification failed: {target} state is {state!r}, "
            "expected RUNNABLE"
        )

    if password is None:
        log.info(
            "Verification (API-level) passed: %s is RUNNABLE. SQL check "
            "skipped — no password configured (enable the Secret Manager "
            "password reset for full verification).", target,
        )
        return

    conn_name = inst.get("connectionName") or f"{target.project}:{cfg.region}:{target.instance}"
    try:
        _verify_sql(conn_name, password)
    except Exception as exc:  # noqa: BLE001
        raise SyncError(
            f"Post-restore SQL verification failed on {target}: {exc}"
        ) from exc
    log.info("Verification passed: %s is RUNNABLE and serving SQL.", target)


def _run_sql(connection_name: str, password: str, statements: list,
             user: str = "postgres") -> None:
    """Execute statements as `user` over the Cloud SQL Python Connector.

    Role membership is cluster-wide in PostgreSQL, so a single connection to
    the `postgres` database is enough — no per-database looping needed.
    """
    from google.cloud.sql.connector import Connector

    with Connector() as connector:
        conn = connector.connect(
            connection_name, "pg8000",
            user=user, password=password, db="postgres",
        )
        try:
            cur = conn.cursor()
            for stmt in statements:
                log.info("  SQL: %s", stmt)
                cur.execute(stmt)
            conn.commit()
        finally:
            conn.close()


def _ensure_iam_db_user(service, target: Target, principal: str, cfg: Config) -> None:
    """Create the IAM database user on the target if it does not exist.

    IAM DB users must be created through the Cloud SQL Admin API (not raw SQL)
    so Cloud SQL wires up IAM authentication for the resulting role.
    """
    user_type = ("CLOUD_IAM_SERVICE_ACCOUNT"
                 if principal.strip().endswith(_IAM_SA_SUFFIX)
                 else "CLOUD_IAM_USER")
    try:
        op = (
            service.users()
            .insert(project=target.project, instance=target.instance,
                    body={"name": principal, "type": user_type})
            .execute(num_retries=3)
        )
    except HttpError as exc:
        if exc.status_code == 409:
            log.info("  IAM DB user %s already present on %s", principal, target)
            return
        _raise_for_http_error(exc, f"creating IAM DB user {principal} on {target}")

    op_name = _extract_op_name(op, "users.insert")
    wait_for_operation(service, target.project, op_name, cfg)
    log.info("  Created IAM DB user %s on %s", principal, target)


def _create_ephemeral_admin(service, target: Target, cfg: Config) -> str:
    """Create a short-lived admin user with an in-memory random password.

    Lets permission mapping run without this tool ever owning the `postgres`
    password. Cloud SQL grants API-created PostgreSQL users cloudsqlsuperuser,
    which is what allows them to GRANT roles.
    """
    password = secrets.token_urlsafe(32)
    body = {"name": _EPHEMERAL_ADMIN_USER, "password": password}
    try:
        op = (
            service.users()
            .insert(project=target.project, instance=target.instance, body=body)
            .execute(num_retries=3)
        )
    except HttpError as exc:
        if exc.status_code == 409:
            # Left behind by a previous crashed run — take it over by setting a
            # password we know, rather than failing the sync.
            log.warning(
                "Ephemeral admin user already exists on %s (leftover from an "
                "interrupted run) — resetting its password to reclaim it.", target,
            )
            op = (
                service.users()
                .update(project=target.project, instance=target.instance,
                        name=_EPHEMERAL_ADMIN_USER, body=body)
                .execute(num_retries=3)
            )
        else:
            _raise_for_http_error(exc, f"creating ephemeral admin user on {target}")

    op_name = _extract_op_name(op, "users.insert (ephemeral admin)")
    wait_for_operation(service, target.project, op_name, cfg)
    log.info("  Created ephemeral admin user on %s.", target)
    return password


def _delete_ephemeral_admin(service, target: Target, cfg: Config) -> None:
    """Best-effort teardown — a cleanup failure must never fail the sync."""
    try:
        op = (
            service.users()
            .delete(project=target.project, instance=target.instance,
                    name=_EPHEMERAL_ADMIN_USER)
            .execute(num_retries=3)
        )
        op_name = op.get("name")
        if op_name:
            wait_for_operation(service, target.project, op_name, cfg)
        log.info("  Removed ephemeral admin user from %s.", target)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Could not remove ephemeral admin user '%s' from %s: %s — "
            "delete it manually.", _EPHEMERAL_ADMIN_USER, target, exc,
        )


def apply_permissions(service, target: Target, cfg: Config, password=None) -> None:
    """Apply cross-project permission mappings and standalone grants.

    Two independent capabilities, both expressed as PostgreSQL role membership:

    - permission_mappings: GRANT "<prod-identity>" TO "<target-identity>".
      The restored prod role still holds every privilege it had in prod, so a
      single membership grant transfers all of them — across every database —
      without enumerating object grants.
    - permission_grants: GRANT "<named-role>" TO "<identity>" for identities
      that have no prod counterpart (e.g. a QA reader getting pg_read_all_data).

    revoke_source_login then does ALTER ROLE ... NOLOGIN on each mapped source
    role. We deliberately do NOT drop it: the target inherits its privileges
    THROUGH that role, so dropping it would revoke exactly what we just granted.
    """
    if not cfg.permission_mappings and not cfg.permission_grants:
        return

    # Pick the admin credential used to run the GRANTs.
    #   postgres  — reuse the Secret Manager password (requires it)
    #   ephemeral — create a throwaway admin user, delete it afterwards
    #   auto      — postgres when a password is available, else ephemeral
    mode = cfg.permission_admin_mode
    if mode == "postgres" and password is None:
        raise SyncError(
            "permission_admin_mode='postgres' needs NONPROD_DB_PASSWORD_SECRET set "
            "(after a restore the target's postgres password is prod's, so the job "
            "must set one it knows). Use permission_admin_mode='ephemeral' to run "
            "the grants as a throwaway admin user instead."
        )
    use_ephemeral = mode == "ephemeral" or (mode == "auto" and password is None)

    log.info(
        "Applying permission mappings/grants on %s (admin: %s) ...",
        target, _EPHEMERAL_ADMIN_USER if use_ephemeral else "postgres",
    )

    # 1. Make sure every target-side identity exists as an IAM DB user.
    for mapping in cfg.permission_mappings:
        _ensure_iam_db_user(service, target, mapping["to"], cfg)
    for grant in cfg.permission_grants:
        _ensure_iam_db_user(service, target, grant["identity"], cfg)

    # 2. Build the statement list.
    statements = []
    for mapping in cfg.permission_mappings:
        src = _quote_ident(_iam_role_name(mapping["from"]))
        dst = _quote_ident(_iam_role_name(mapping["to"]))
        statements.append(f"GRANT {src} TO {dst}")
    for grant in cfg.permission_grants:
        who = _quote_ident(_iam_role_name(grant["identity"]))
        for role in grant["roles"]:
            statements.append(f"GRANT {_quote_ident(role)} TO {who}")
    if cfg.revoke_source_login:
        for mapping in cfg.permission_mappings:
            src = _quote_ident(_iam_role_name(mapping["from"]))
            statements.append(f"ALTER ROLE {src} NOLOGIN")

    # 3. Run them.
    try:
        inst = (
            service.instances()
            .get(project=target.project, instance=target.instance)
            .execute(num_retries=3)
        )
    except HttpError as exc:
        _raise_for_http_error(exc, f"reading {target} for permission mapping")
    conn_name = inst.get("connectionName") or f"{target.project}:{cfg.region}:{target.instance}"

    admin_user = "postgres"
    admin_password = password
    if use_ephemeral:
        admin_user = _EPHEMERAL_ADMIN_USER
        admin_password = _create_ephemeral_admin(service, target, cfg)

    try:
        _run_sql(conn_name, admin_password, statements, user=admin_user)
    except Exception as exc:  # noqa: BLE001
        raise SyncError(f"Permission mapping failed on {target}: {exc}") from exc
    finally:
        if use_ephemeral:
            _delete_ephemeral_admin(service, target, cfg)
    log.info("Permissions applied on %s (%d statement(s)).", target, len(statements))


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
# Secret Manager — nonprod password reset
# ---------------------------------------------------------------------------

def _fetch_secret(secret_resource_name: str) -> str:
    """Fetch the latest version of a Secret Manager secret."""
    client = secretmanager.SecretManagerServiceClient()
    name = f"{secret_resource_name}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


def reset_target_password(service, target: Target, cfg: Config, password: str) -> None:
    """Reset one target's postgres user password to the supplied value.

    After a restore the target instance has prod's password. This step resets
    it to a known, separately-managed credential so the nonprod apps continue
    to work without reconfiguration.

    Uses the Cloud SQL Admin API (no direct DB connection required).
    """
    log.info("Resetting postgres password on %s ...", target)
    try:
        op = (
            service.users()
            .update(
                project=target.project,
                instance=target.instance,
                name="postgres",
                body={"name": "postgres", "password": password},
            )
            .execute(num_retries=3)
        )
    except HttpError as exc:
        _raise_for_http_error(exc, f"resetting postgres password on {target}")

    # users.update returns an Operation — poll until done.
    op_name = _extract_op_name(op, "users.update")
    wait_for_operation(service, target.project, op_name, cfg)
    log.info("Password reset on %s complete.", target)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()
    # Optional: reset each target's DB password after restore using Secret Manager.
    # Set NONPROD_DB_PASSWORD_SECRET to the full secret resource name:
    #   projects/PROJECT/secrets/SECRET_NAME
    password_secret = os.getenv("NONPROD_DB_PASSWORD_SECRET", "").strip()

    targets = cfg.nonprod_targets
    log.info(
        "Starting sync: %s/%s → %d target(s): %s  (region: %s)",
        cfg.prod_project, cfg.prod_instance,
        len(targets), ", ".join(str(t) for t in targets),
        cfg.region,
    )
    log.info(
        "Config: poll_interval=%ds  timeout=%ds  password_reset=%s  backup=%s",
        cfg.poll_interval, cfg.operation_timeout,
        "enabled" if password_secret else "disabled",
        "reuse-latest-existing" if cfg.use_latest_existing_backup else "create-new",
    )

    # SYNC_NETWORK_MODE is stamped by the deploy paths ("private" when a VPC
    # connector / Direct VPC egress is configured). Anything else = public.
    if os.getenv("SYNC_NETWORK_MODE", "public").strip().lower() != "private":
        log.warning(
            "Job egress is PUBLIC — Cloud SQL Admin API traffic traverses the "
            "public internet. For production, configure private networking "
            "(vpc_connector or vpc_network in config) — see README 'Networking'."
        )

    service = build_sqladmin()

    # Fetch the password once and reuse for every target (it's the same secret).
    password = None
    if password_secret:
        try:
            password = _fetch_secret(password_secret)
        except Exception as exc:  # noqa: BLE001
            raise SyncError(
                f"Failed to read secret {password_secret}: {exc}"
            ) from exc

    # Post-restore verification is on by default; VERIFY_RESTORE=false skips it.
    verify_enabled = _bool_env("VERIFY_RESTORE", default=True)

    # Either create a fresh backup (owned, will be deleted) or reuse the latest
    # existing one (not owned, left in place).
    backup_id, backup_owned = acquire_backup(service, cfg)

    # Restore (and optionally reset password) for each target. A failure on
    # one target is logged and recorded but does not block the others — we
    # attempt all targets, then fail at the end if any failed.
    failures: list = []
    try:
        for target in targets:
            try:
                restore_to_target(service, backup_id, target, cfg)
                if password is not None:
                    reset_target_password(service, target, cfg, password)
                apply_permissions(service, target, cfg, password)
                if verify_enabled:
                    verify_target(service, target, cfg, password)
            except SyncError as exc:
                log.error("Target %s failed: %s", target, exc)
                failures.append((target, exc))
    finally:
        # Only delete backups WE created — never a pre-existing one.
        if backup_owned:
            delete_backup(service, backup_id, cfg)
        else:
            log.info("Backup %d was pre-existing — leaving it in place.", backup_id)

    if failures:
        names = ", ".join(str(t) for t, _ in failures)
        raise SyncError(
            f"{len(failures)} of {len(targets)} target(s) failed: {names}"
        )

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
