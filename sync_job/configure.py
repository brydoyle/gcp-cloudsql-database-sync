#!/usr/bin/env python3
"""
Interactive configuration wizard for the CloudSQL sync job.

Usage:
  python3 configure.py              # interactive prompts
  python3 configure.py --file config.yaml   # load existing config, prompt for missing/changed values
  python3 configure.py --non-interactive    # validate existing config.yaml and exit

Writes (or updates) config.yaml in the same directory.
deploy.sh reads config.yaml automatically if it exists.
"""

import argparse
import os
import re
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Optional PyYAML — fall back to a simple writer if not installed
# ---------------------------------------------------------------------------
try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# ---------------------------------------------------------------------------
# Validation (mirrors main.py)
# ---------------------------------------------------------------------------

_PROJECT_ID_RE   = re.compile(r"^[a-z][a-z0-9\-]{4,28}[a-z0-9]$")
_INSTANCE_NAME_RE = re.compile(r"^[a-z]([a-z0-9\-]{0,96}[a-z0-9])?$")
_CRON_RE         = re.compile(r"^(\*|[0-9,\-\*/]+)\s+(\*|[0-9,\-\*/]+)\s+(\*|[0-9,\-\*/]+)\s+(\*|[0-9,\-\*/]+)\s+(\*|[0-9,\-\*/]+)$")

GCP_REGIONS = {
    "us-central1", "us-east1", "us-east4", "us-east5", "us-south1",
    "us-west1", "us-west2", "us-west3", "us-west4",
    "northamerica-northeast1", "northamerica-northeast2",
    "southamerica-east1", "southamerica-west1",
    "europe-central2", "europe-north1", "europe-southwest1",
    "europe-west1", "europe-west2", "europe-west3", "europe-west4",
    "europe-west6", "europe-west8", "europe-west9", "europe-west10", "europe-west12",
    "asia-east1", "asia-east2",
    "asia-northeast1", "asia-northeast2", "asia-northeast3",
    "asia-south1", "asia-south2",
    "asia-southeast1", "asia-southeast2",
    "australia-southeast1", "australia-southeast2",
    "me-central1", "me-central2", "me-west1",
    "africa-south1",
}

TIMEZONES = {
    "UTC", "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Toronto", "America/Vancouver",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Dublin",
    "Asia/Tokyo", "Asia/Singapore", "Asia/Sydney", "Australia/Sydney",
}

# ---------------------------------------------------------------------------
# Schedule presets / builder
# ---------------------------------------------------------------------------

# Sentinel meaning "no Cloud Scheduler — run on demand only".
ON_DEMAND = "on-demand"

# cron day-of-week numbers (0 = Sunday … 6 = Saturday).
_WEEKDAYS = {
    "sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3,
    "thursday": 4, "friday": 5, "saturday": 6,
}
_WEEKDAY_BY_NUM = {v: k for k, v in _WEEKDAYS.items()}

# Default: every Saturday night at 23:00.
DEFAULT_SCHEDULE = "0 23 * * 6"

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]?\d)$")


def _parse_hhmm(text: str):
    """Parse 'HH:MM' (24h) → (hour, minute), or None if invalid."""
    m = _HHMM_RE.match(text.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _build_cron(hour: int, minute: int, *, weekday=None, weekdays_only=False) -> str:
    """Assemble a 5-field cron string from time-of-day parts."""
    if weekdays_only:
        dow = "1-5"
    elif weekday is not None:
        dow = str(weekday)
    else:
        dow = "*"
    return f"{minute} {hour} * * {dow}"


def _validate_schedule_value(v: str):
    """Accept the on-demand sentinel or any 5-field cron expression."""
    if v.strip().lower() == ON_DEMAND:
        return None
    if _CRON_RE.match(v.strip()):
        return None
    return f"Must be a 5-field cron expression or '{ON_DEMAND}'"


def _describe_schedule(value: str) -> str:
    """Human-friendly one-liner for a stored schedule value (for summaries)."""
    if not value or value.strip().lower() == ON_DEMAND:
        return "on-demand (no scheduler)"
    return f"cron: {value}"

# Named validators — shared across the fields that use the same rule,
# so the error message and regex live in exactly one place each.
def _validate_project_id(v: str) -> Optional[str]:
    return (None if _PROJECT_ID_RE.match(v) else
            "Must be 6–30 chars, start with a lowercase letter, letters/digits/hyphens only")


def _validate_instance_name(v: str) -> Optional[str]:
    return (None if _INSTANCE_NAME_RE.match(v) else
            "Must start with a lowercase letter, letters/digits/hyphens only, 1–98 chars")


FIELDS = [
    {
        "key":      "prod_project_id",
        "label":    "Production GCP Project ID",
        "example":  "my-prod-project",
        "validate": _validate_project_id,
    },
    {
        "key":      "prod_instance_name",
        "label":    "Production Cloud SQL Instance Name",
        "example":  "prod-db",
        "validate": _validate_instance_name,
    },
    {
        "key":      "nonprod_project_id",
        "label":    "Non-Production GCP Project ID",
        "example":  "my-nonprod-project",
        "validate": _validate_project_id,
    },
    {
        "key":      "nonprod_instance_name",
        "label":    "Non-Production Cloud SQL Instance Name",
        "example":  "nonprod-db",
        "validate": _validate_instance_name,
    },
    {
        "key":     "region",
        "label":   "GCP Region",
        "default": "us-central1",
        "validate": lambda v: (
            f"Not a recognised GCP region. Examples: us-central1, europe-west1, asia-east1"
            if v not in GCP_REGIONS else None
        ),
    },
    {
        "key":      "schedule",
        "label":    "Sync schedule",
        "default":  DEFAULT_SCHEDULE,  # every Saturday night
        "validate": _validate_schedule_value,
        # Prompted via the interactive _prompt_schedule() builder, not the
        # generic text prompt — see main().
    },
    {
        "key":     "timezone",
        "label":   "Schedule timezone",
        "default": "UTC",
        "hint":    f"Common values: {', '.join(sorted(TIMEZONES)[:6])} ...",
        "validate": lambda v: None,   # GCP accepts any valid tz string; warn only
    },
    {
        "key":     "job_name",
        "label":   "Cloud Run Job name",
        "default": "cloudsql-sync",
        "validate": lambda v: (
            "Must start with a lowercase letter, letters/digits/hyphens only, 1–49 chars"
            if not re.match(r"^[a-z][a-z0-9\-]{0,48}$", v) else None
        ),
    },
    {
        "key":      "alert_email",
        "label":    "Alert email address (failures will be sent here)",
        "hint":     "Leave blank to skip monitoring setup",
        "optional": True,
        "validate": lambda v: (
            "Must be a valid email address"
            if v and not re.match(r"^[^@]+@[^@]+\.[^@]+$", v) else None
        ),
    },
    {
        "key":      "use_latest_existing_backup",
        "label":    "Reuse the most recent existing backup instead of creating one?",
        "hint":     "true/false — when true, the reused backup is never deleted (default: false)",
        "default":  "false",
        "validate": lambda v: (
            "Must be 'true' or 'false'"
            if v.lower() not in ("true", "false") else None
        ),
    },
    {
        "key":      "verify_restore",
        "label":    "Verify each target after restore?",
        "hint":     "true/false — API RUNNABLE check always; live SQL check when password reset is enabled (default: true)",
        "default":  "true",
        "validate": lambda v: (
            "Must be 'true' or 'false'"
            if v.lower() not in ("true", "false") else None
        ),
    },
    # ── Optional private networking for the Cloud Run Job ────────────────────
    # Leave all blank for public egress (works in any environment). Set EITHER
    # a Serverless VPC Access connector OR a network for Direct VPC egress.
    {
        "key":      "vpc_connector",
        "label":    "VPC connector for the job (private networking)",
        "hint":     "Leave blank for public egress. Mutually exclusive with vpc network.",
        "optional": True,
        "validate": lambda v: None,  # name or full resource ID — API validates
    },
    {
        "key":      "vpc_network",
        "label":    "VPC network for Direct VPC egress",
        "hint":     "Leave blank for public egress. Mutually exclusive with vpc connector.",
        "optional": True,
        "validate": lambda v: None,
    },
    {
        "key":      "vpc_subnetwork",
        "label":    "Subnetwork for Direct VPC egress",
        "hint":     "Only used with a vpc network; blank = network's subnet in the job region",
        "optional": True,
        "validate": lambda v: None,
    },
    {
        "key":      "vpc_egress",
        "label":    "VPC egress mode (when VPC networking is set)",
        "hint":     "private-ranges-only (default) or all-traffic (subnet needs Private Google Access)",
        "default":  "private-ranges-only",
        "validate": lambda v: (
            "Must be 'private-ranges-only' or 'all-traffic'"
            if v.lower() not in ("private-ranges-only", "all-traffic") else None
        ),
    },
]


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def _load_yaml(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    if _HAS_YAML:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    # Minimal parser for simple key: value lines
    config = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and ":" in line:
                k, _, v = line.partition(":")
                # Strip surrounding whitespace and quotes so values like
                # schedule: "0 2 * * *" load as  0 2 * * *
                config[k.strip()] = v.strip().strip("\"'")
    return config


def _write_yaml(path: str, config: dict) -> None:
    if _HAS_YAML:
        with open(path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        return
    # Minimal writer
    with open(path, "w") as f:
        f.write("# CloudSQL Sync Configuration\n")
        f.write("# Generated by configure.py — edit carefully\n\n")
        for k, v in config.items():
            f.write(f"{k}: {v}\n")


# ---------------------------------------------------------------------------
# Terraform tfvars writer
# ---------------------------------------------------------------------------

def _write_tfvars(path: str, config: dict) -> None:
    """Write a terraform.tfvars file from the config dict.

    Defaults for optional fields are derived from FIELDS so they stay in sync
    automatically when FIELDS is updated.
    """
    # Build a merged view: FIELDS defaults < supplied config values.
    _field_defaults = {f["key"]: f.get("default", "") for f in FIELDS}
    cfg = {**_field_defaults, **config}

    nonprod_project = cfg["nonprod_project_id"]
    job_name        = cfg["job_name"]
    alert_email     = cfg.get("alert_email", "")
    image = f"gcr.io/{nonprod_project}/{job_name}:latest"
    email_line = (f'alert_email = "{alert_email}"'
                  if alert_email else
                  '# alert_email = "your-team@example.com"')
    # Terraform expects unquoted bool literals.
    use_latest = str(cfg.get("use_latest_existing_backup", "false")).lower() == "true"
    verify     = str(cfg.get("verify_restore", "true")).lower() != "false"

    # Optional networking → emit only what's set; map egress to the TF enum.
    vpc_lines = []
    if cfg.get("vpc_connector"):
        vpc_lines.append(f'vpc_connector = "{cfg["vpc_connector"]}"')
    if cfg.get("vpc_network"):
        vpc_lines.append(f'vpc_network = "{cfg["vpc_network"]}"')
    if cfg.get("vpc_subnetwork"):
        vpc_lines.append(f'vpc_subnetwork = "{cfg["vpc_subnetwork"]}"')
    if vpc_lines:
        egress = str(cfg.get("vpc_egress", "private-ranges-only")).upper().replace("-", "_")
        vpc_lines.append(f'vpc_egress = "{egress}"')
    vpc_block = ("\n" + "\n".join(vpc_lines) + "\n") if vpc_lines else \
        "\n# Public egress (no VPC). Set vpc_connector or vpc_network for private networking.\n"

    with open(path, "w") as f:
        f.write(f"""\
# CloudSQL Sync — Terraform variables
# Generated by configure.py — do not commit this file

prod_project_id       = "{cfg['prod_project_id']}"
prod_instance_name    = "{cfg['prod_instance_name']}"

nonprod_project_id    = "{nonprod_project}"
nonprod_instance_name = "{cfg['nonprod_instance_name']}"

region     = "{cfg['region']}"
job_name   = "{job_name}"
schedule   = "{cfg['schedule']}"
timezone   = "{cfg['timezone']}"

use_latest_existing_backup = {"true" if use_latest else "false"}
verify_restore             = {"true" if verify else "false"}
{vpc_block}
# Build image first: gcloud builds submit sync_job/ --tag={image}
container_image = "{image}"

{email_line}
""")

    print(f"  Also saved Terraform vars to {path}")


# ---------------------------------------------------------------------------
# Schedule prompt (interactive builder)
# ---------------------------------------------------------------------------

def _ask(prompt_str: str, default: str = "") -> str:
    """Single input line with EOF/Ctrl-C handling; returns default on blank."""
    shown = f"{prompt_str} [{default}] " if default else f"{prompt_str} "
    try:
        raw = input(shown).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)
    return raw or default


def _ask_time(default_hhmm: str) -> tuple:
    """Prompt for an HH:MM time, re-asking until valid. Returns (hour, minute)."""
    while True:
        parsed = _parse_hhmm(_ask("    Time of day (HH:MM, 24-hour):", default_hhmm))
        if parsed:
            return parsed
        print("    Invalid time — use HH:MM (e.g. 23:00, 02:30).")


def _prompt_schedule(current: Optional[str], non_interactive: bool) -> str:
    """Interactive schedule builder. Returns a cron string or the ON_DEMAND
    sentinel. Far friendlier than asking for raw cron."""
    default = current or DEFAULT_SCHEDULE

    if non_interactive:
        err = _validate_schedule_value(default)
        if err:
            print(f"  ERROR: schedule — {err} (got: {default!r})", file=sys.stderr)
            sys.exit(1)
        return default

    print()
    print("  Sync schedule")
    if current:
        keep = _ask(f"  Keep current ({_describe_schedule(current)})? [Y/n]", "Y")
        if keep.lower() in ("y", "yes"):
            return current

    print("  How often should the sync run?")
    print("    1) Weekly        (pick a day + time)   ← default")
    print("    2) Daily         (pick a time)")
    print("    3) Weekdays      (Mon–Fri, pick a time)")
    print("    4) On-demand     (no scheduler — trigger manually / CI)")
    print("    5) Custom cron   (enter a raw 5-field expression)")

    choice = _ask("  Choose 1-5:", "1").lower()

    # Weekly
    if choice in ("1", "weekly", ""):
        day = _ask("    Day of week:", "saturday").lower()
        while day not in _WEEKDAYS:
            print(f"    Pick one of: {', '.join(_WEEKDAYS)}")
            day = _ask("    Day of week:", "saturday").lower()
        hour, minute = _ask_time("23:00")  # "Saturday night"
        return _build_cron(hour, minute, weekday=_WEEKDAYS[day])

    # Daily
    if choice in ("2", "daily"):
        hour, minute = _ask_time("02:00")
        return _build_cron(hour, minute)

    # Weekdays
    if choice in ("3", "weekdays"):
        hour, minute = _ask_time("02:00")
        return _build_cron(hour, minute, weekdays_only=True)

    # On-demand
    if choice in ("4", "on-demand", "none"):
        return ON_DEMAND

    # Custom cron
    while True:
        cron = _ask("    Raw cron (5 fields, e.g. '0 23 * * 6'):", DEFAULT_SCHEDULE)
        if _CRON_RE.match(cron):
            return cron
        print("    Invalid cron — need 5 space-separated fields.")


# ---------------------------------------------------------------------------
# Prompt helper
# ---------------------------------------------------------------------------

def _prompt(field: dict, current: Optional[str], non_interactive: bool) -> str:
    default = current or field.get("default", "")
    hint    = field.get("hint", "")
    example = field.get("example", "")

    optional = field.get("optional", False)

    if non_interactive:
        if not default and not optional:
            print(f"  ERROR: {field['label']} is required but not set.", file=sys.stderr)
            sys.exit(1)
        # Validate even in non-interactive mode so bad config.yaml values are caught.
        if default:
            error = field["validate"](default)
            if error:
                print(f"  ERROR: {field['label']} — {error} (got: {default!r})", file=sys.stderr)
                sys.exit(1)
        return default

    print()
    print(f"  {field['label']}")
    if hint:
        print(f"  ({hint})")
    if example and not default:
        print(f"  Example: {example}")

    while True:
        prompt_str = f"  > [{default}] " if default else f"  > "
        try:
            raw = input(prompt_str).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)

        value = raw or default
        if not value:
            if optional:
                return ""
            print(f"  This field is required.")
            continue

        error = field["validate"](value)
        if error:
            print(f"  Invalid: {error}")
            continue

        return value


# ---------------------------------------------------------------------------
# Safety check
# ---------------------------------------------------------------------------

def _check_vpc_exclusive(config: dict) -> None:
    if config.get("vpc_connector") and config.get("vpc_network"):
        print(
            "\n  ERROR: vpc_connector and vpc_network are mutually exclusive — "
            "set one (connector egress) or the other (Direct VPC egress), not both.",
            file=sys.stderr,
        )
        sys.exit(1)


def _check_prod_ne_nonprod(config: dict) -> None:
    if (config.get("prod_project_id") == config.get("nonprod_project_id") and
            config.get("prod_instance_name") == config.get("nonprod_instance_name")):
        print(
            "\n  ERROR: PROD and NONPROD point to the same instance. "
            "This would overwrite production data.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Configure the CloudSQL sync job.")
    parser.add_argument(
        "--file", default=os.path.join(os.path.dirname(__file__), "config.yaml"),
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Validate existing config.yaml and exit without prompting.",
    )
    args = parser.parse_args()

    config_path = args.file
    non_interactive = args.non_interactive

    print("=" * 60)
    print("  CloudSQL Sync — Configuration Wizard")
    print("=" * 60)

    if not _HAS_YAML:
        print("\n  Note: PyYAML not installed. Using simple key:value format.")
        print("  Install with: pip install pyyaml\n")

    existing = _load_yaml(config_path)
    if existing:
        print(f"\n  Found existing config at {config_path}")
        if non_interactive:
            print("  Running in non-interactive mode — validating only.\n")
    elif non_interactive:
        print(f"\n  ERROR: No config found at {config_path}. Run without --non-interactive first.", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"\n  No existing config found. Starting fresh.\n")

    config = {}
    for field in FIELDS:
        current = existing.get(field["key"])
        if field["key"] == "schedule":
            config["schedule"] = _prompt_schedule(current, non_interactive)
        else:
            config[field["key"]] = _prompt(field, current, non_interactive)

    _check_prod_ne_nonprod(config)
    _check_vpc_exclusive(config)

    if not non_interactive:
        print()
        print("=" * 60)
        print("  Summary")
        print("=" * 60)
        for field in FIELDS:
            value = config[field["key"]]
            if field["key"] == "schedule":
                value = _describe_schedule(value)
            print(f"  {field['label']:45s} {value}")
        print()

        try:
            confirm = input("  Save to config.yaml? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)

        if confirm not in ("", "y", "yes"):
            print("  Aborted — nothing saved.")
            sys.exit(0)

        _write_yaml(config_path, config)
        print(f"\n  Saved to {config_path}")

        # Also generate terraform/terraform.tfvars if the terraform dir exists.
        tf_dir = os.path.join(os.path.dirname(__file__), "..", "terraform")
        if os.path.isdir(tf_dir):
            _write_tfvars(os.path.join(tf_dir, "terraform.tfvars"), config)

        print("  Run 'bash deploy.sh' to deploy with bash, or")
        print("  'cd terraform && terraform init && terraform apply' for Terraform.\n")
    else:
        print("  Config is valid.\n")


if __name__ == "__main__":
    main()
