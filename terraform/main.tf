# ---------------------------------------------------------------------------
# CloudSQL Cross-Project Sync — Terraform Module
#
# Manages:
#   - Service accounts (job + scheduler)
#   - IAM bindings (both projects)
#   - Cloud Run Job
#   - Cloud Scheduler trigger
#   - Cloud Monitoring alert + email notification on failure
#
# Prerequisites:
#   - Container image already built and pushed (see container_image variable)
#   - Required APIs enabled (see locals.apis below)
# ---------------------------------------------------------------------------

locals {
  apis = [
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "sqladmin.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
  ]

  # Restore targets: the explicit list if provided, else the single pair.
  targets = length(var.nonprod_targets) > 0 ? var.nonprod_targets : [
    { project = var.nonprod_project_id, instance = var.nonprod_instance_name }
  ]

  # Distinct target projects that need the job SA's cloudsql.admin binding.
  target_projects = distinct([for t in local.targets : t.project])

  # Comma-separated "project:instance" string consumed by main.py.
  nonprod_targets_env = join(",", [for t in local.targets : "${t.project}:${t.instance}"])

  # On-demand mode (schedule == "on-demand" or empty) creates no scheduler.
  create_scheduler = !contains(["", "on-demand"], var.schedule)

  # Private networking is opt-in: connector OR direct VPC egress. Blank = public.
  use_vpc = var.vpc_connector != "" || var.vpc_network != ""

  # Absence-alert window derived from the schedule cadence (plus slack for a
  # slow run), so a weekly schedule doesn't page daily. Cloud Monitoring caps
  # absence durations at 24.5 days. No absence alert at all when on-demand.
  sched_fields = split(" ", trimspace(var.schedule))
  sched_dom    = length(local.sched_fields) == 5 ? local.sched_fields[2] : "*"
  sched_dow    = length(local.sched_fields) == 5 ? local.sched_fields[4] : "*"

  absence_seconds = (
    local.sched_dow == "*" && local.sched_dom == "*" ? 90000 : # daily/finer: 25h
    local.sched_dow == "1-5" ? 262800 :                        # weekdays: 73h (weekend gap)
    local.sched_dom != "*" ? 2116800 :                         # monthly: 24.5d (API max)
    691200                                                     # weekly: 8 days
  )
  absence_label = (
    local.absence_seconds == 90000 ? "25 hours" :
    local.absence_seconds == 262800 ? "73 hours" :
    local.absence_seconds == 2116800 ? "24.5 days" :
    "8 days"
  )
}

# ── Guard: detect a prior deploy.sh deployment ────────────────────────────────
#
# deploy.sh and this module manage the SAME resources — running both against
# one project causes ownership collisions. deploy.sh labels its Cloud Run job
# `managed-by=deploy-sh`; if we find that label, fail loudly with guidance.
#
# Semantics:
#   - Fresh project (no job yet): the data source 404s. Inside a `check` block
#     that surfaces as a one-time WARNING, not an error — apply proceeds.
#   - Terraform-managed job: no `managed-by=deploy-sh` label → assertion passes.
#   - deploy.sh-managed job: label present → assertion FAILS with instructions.
check "no_bash_deploy" {
  data "google_cloud_run_v2_job" "existing" {
    name     = var.job_name
    location = var.region
    project  = var.nonprod_project_id
  }

  assert {
    condition = lookup(
      data.google_cloud_run_v2_job.existing.effective_labels, "managed-by", ""
    ) != "deploy-sh"
    error_message = join(" ", [
      "Cloud Run job '${var.job_name}' was deployed by deploy.sh",
      "(label managed-by=deploy-sh). Terraform and deploy.sh must not manage",
      "the same resources. Either import the existing resources into Terraform",
      "state, or tear down the deploy.sh resources first.",
      "See README → 'Choosing a deploy path'.",
    ])
  }
}

# ── Warning: public egress ────────────────────────────────────────────────────
# A failed check assertion is a WARNING (not an error): every plan/apply on a
# public-egress config surfaces this, without blocking POC use.
check "public_networking" {
  assert {
    condition     = local.use_vpc
    error_message = "Job egress is PUBLIC (no vpc_connector or vpc_network set). Fine for a POC; for production configure private networking — see README 'Networking'."
  }
}

# ── APIs ─────────────────────────────────────────────────────────────────────

resource "google_project_service" "nonprod_apis" {
  for_each = toset(local.apis)
  project  = var.nonprod_project_id
  service  = each.value

  disable_on_destroy = false
}

resource "google_project_service" "prod_sqladmin" {
  provider = google.prod
  project  = var.prod_project_id
  service  = "sqladmin.googleapis.com"

  disable_on_destroy = false
}

# ── Job service account ───────────────────────────────────────────────────────

resource "google_service_account" "job" {
  account_id   = var.job_name
  display_name = "CloudSQL Sync Job"
  project      = var.nonprod_project_id

  depends_on = [google_project_service.nonprod_apis]
}

# ── IAM: least-privilege custom roles (default) or cloudsql.admin fallback ────
#
# With least_privilege=true (default) the job SA gets exactly the permissions
# the sync uses — nothing that could delete or reconfigure an instance:
#   prod:   create/read/list/delete backup runs, read operations & instance
#   target: restore, read instance, connect (SQL verification), update the
#           postgres user (password reset), read operations
# Role IDs are derived from job_name (custom role IDs allow only [a-zA-Z0-9_.]).

locals {
  role_id_base = replace(var.job_name, "-", "_")
}

resource "google_project_iam_custom_role" "prod_backup_ops" {
  count       = var.least_privilege ? 1 : 0
  provider    = google.prod
  project     = var.prod_project_id
  role_id     = "${local.role_id_base}_backup_ops"
  title       = "CloudSQL Sync — prod backup operations"
  description = "Create/read/list/delete backup runs and read operations for the ${var.job_name} job. No instance mutation."
  permissions = [
    "cloudsql.backupRuns.create",
    "cloudsql.backupRuns.get",
    "cloudsql.backupRuns.list",
    "cloudsql.backupRuns.delete",
    "cloudsql.operations.get",
    "cloudsql.instances.get",
  ]
}

resource "google_project_iam_member" "job_prod_backup_ops" {
  count    = var.least_privilege ? 1 : 0
  provider = google.prod
  project  = var.prod_project_id
  role     = google_project_iam_custom_role.prod_backup_ops[0].id
  member   = "serviceAccount:${google_service_account.job.email}"
}

resource "google_project_iam_custom_role" "target_restore_ops" {
  for_each    = var.least_privilege ? toset(local.target_projects) : toset([])
  project     = each.value
  role_id     = "${local.role_id_base}_restore_ops"
  title       = "CloudSQL Sync — target restore operations"
  description = "Restore, verify, and reset the postgres password on sync targets for the ${var.job_name} job."
  permissions = [
    "cloudsql.instances.restoreBackup",
    "cloudsql.instances.get",
    "cloudsql.instances.connect",
    "cloudsql.users.update",
    "cloudsql.users.list",
    "cloudsql.operations.get",
  ]
}

resource "google_project_iam_member" "job_target_restore_ops" {
  for_each = var.least_privilege ? toset(local.target_projects) : toset([])
  project  = each.value
  role     = google_project_iam_custom_role.target_restore_ops[each.value].id
  member   = "serviceAccount:${google_service_account.job.email}"
}

# Fallback: broad cloudsql.admin (pre-least-privilege behavior).
resource "google_project_iam_member" "job_prod_cloudsql_admin" {
  count    = var.least_privilege ? 0 : 1
  provider = google.prod
  project  = var.prod_project_id
  role     = "roles/cloudsql.admin"
  member   = "serviceAccount:${google_service_account.job.email}"
}

resource "google_project_iam_member" "job_target_cloudsql_admin" {
  for_each = var.least_privilege ? toset([]) : toset(local.target_projects)
  project  = each.value
  role     = "roles/cloudsql.admin"
  member   = "serviceAccount:${google_service_account.job.email}"
}

# ── Scheduler service account ─────────────────────────────────────────────────

resource "google_service_account" "scheduler" {
  count        = local.create_scheduler ? 1 : 0
  account_id   = "${var.job_name}-scheduler"
  display_name = "CloudSQL Sync Scheduler"
  project      = var.nonprod_project_id

  depends_on = [google_project_service.nonprod_apis]
}

resource "google_project_iam_member" "scheduler_run_invoker" {
  count   = local.create_scheduler ? 1 : 0
  project = var.nonprod_project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.scheduler[0].email}"
}

# ── Cloud Run Job ─────────────────────────────────────────────────────────────

resource "google_cloud_run_v2_job" "sync" {
  name     = var.job_name
  location = var.region
  project  = var.nonprod_project_id

  template {
    template {
      service_account = google_service_account.job.email
      max_retries     = 1

      timeout = "${tostring(var.task_timeout_seconds)}s"

      # Optional private networking — omitted entirely when no VPC vars are
      # set, so the module works in any environment with zero prerequisites.
      dynamic "vpc_access" {
        for_each = local.use_vpc ? [1] : []
        content {
          connector = var.vpc_connector != "" ? var.vpc_connector : null
          egress    = var.vpc_egress

          # Direct VPC egress (used when vpc_network is set instead of a connector)
          dynamic "network_interfaces" {
            for_each = var.vpc_network != "" ? [1] : []
            content {
              network    = var.vpc_network
              subnetwork = var.vpc_subnetwork != "" ? var.vpc_subnetwork : null
            }
          }
        }
      }

      containers {
        image = var.container_image

        env {
          name  = "PROD_PROJECT_ID"
          value = var.prod_project_id
        }
        env {
          name  = "PROD_INSTANCE_NAME"
          value = var.prod_instance_name
        }
        # Multi-target fan-out: "project:instance,project:instance,..."
        # main.py prefers this over the single NONPROD_PROJECT_ID pair.
        env {
          name  = "NONPROD_TARGETS"
          value = local.nonprod_targets_env
        }
        env {
          name  = "GCP_REGION"
          value = var.region
        }
        env {
          name  = "POLL_INTERVAL_SECONDS"
          value = tostring(var.poll_interval_seconds)
        }
        env {
          name  = "OPERATION_TIMEOUT_SECONDS"
          value = tostring(var.task_timeout_seconds)
        }
        env {
          name  = "USE_LATEST_EXISTING_BACKUP"
          value = tostring(var.use_latest_existing_backup)
        }
        # Lets the job warn at runtime when its egress is public.
        env {
          name  = "SYNC_NETWORK_MODE"
          value = local.use_vpc ? "private" : "public"
        }
        env {
          name  = "VERIFY_RESTORE"
          value = tostring(var.verify_restore)
        }
        # Pass the secret RESOURCE NAME (not the value) — main.py fetches the
        # secret itself via the Secret Manager client using the job SA's
        # secretAccessor binding. This keeps both deploy paths consistent.
        env {
          name  = "NONPROD_DB_PASSWORD_SECRET"
          value = google_secret_manager_secret.nonprod_db_password.id
        }
      }
    }
  }

  depends_on = [
    google_project_service.nonprod_apis,
    google_project_iam_member.job_prod_backup_ops,
    google_project_iam_member.job_target_restore_ops,
    google_project_iam_member.job_prod_cloudsql_admin,
    google_project_iam_member.job_target_cloudsql_admin,
  ]

  lifecycle {
    precondition {
      condition     = !(var.vpc_connector != "" && var.vpc_network != "")
      error_message = "vpc_connector and vpc_network are mutually exclusive — set one (connector-based egress) or the other (Direct VPC egress), not both."
    }
  }
}

# ── Cloud Scheduler ───────────────────────────────────────────────────────────

resource "google_cloud_scheduler_job" "nightly" {
  count     = local.create_scheduler ? 1 : 0
  name      = "${var.job_name}-nightly"
  region    = var.region
  project   = var.nonprod_project_id
  schedule  = var.schedule
  time_zone = var.timezone

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.nonprod_project_id}/jobs/${var.job_name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler[0].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_cloud_run_v2_job.sync,
    google_project_iam_member.scheduler_run_invoker,
  ]
}

# ── Monitoring & Alerting ─────────────────────────────────────────────────────

# Log-based metric: count failed Cloud Run Job executions.
# A failed execution logs "Container called exit(N)" where N != 0.
resource "google_logging_metric" "sync_failure" {
  name    = "${var.job_name}-failure"
  project = var.nonprod_project_id

  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=\"${var.job_name}\"",
    "textPayload=~\"Container called exit\\([1-9][0-9]*\\)\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "${var.job_name} failed executions"
  }

  depends_on = [google_project_service.nonprod_apis]
}

# Email notification channel.
resource "google_monitoring_notification_channel" "email" {
  display_name = "CloudSQL Sync Alerts"
  type         = "email"
  project      = var.nonprod_project_id

  labels = {
    email_address = var.alert_email
  }

  depends_on = [google_project_service.nonprod_apis]
}

# Alert policy: fire when any failure is logged.
resource "google_monitoring_alert_policy" "sync_failure" {
  display_name = "${var.job_name} sync failed"
  project      = var.nonprod_project_id
  combiner     = "OR"

  conditions {
    display_name = "Sync job exited with non-zero code"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.sync_failure.name}\" AND resource.type=\"cloud_run_job\""
      duration        = "0s" # alert immediately on first occurrence
      comparison      = "COMPARISON_GT"
      threshold_value = 0

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_COUNT"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  alert_strategy {
    notification_rate_limit {
      period = "3600s" # at most one alert per hour
    }
  }

  documentation {
    content   = "The CloudSQL sync job **${var.job_name}** failed. Check logs: https://console.cloud.google.com/run/jobs/executions?project=${var.nonprod_project_id}"
    mime_type = "text/markdown"
  }

  depends_on = [google_logging_metric.sync_failure]
}

# Log-based metric: count successful Cloud Run Job executions.
resource "google_logging_metric" "sync_success" {
  name    = "${var.job_name}-success"
  project = var.nonprod_project_id

  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=\"${var.job_name}\"",
    "textPayload=\"Sync finished successfully.\"",
  ])

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "${var.job_name} successful executions"
  }

  depends_on = [google_project_service.nonprod_apis]
}

# Alert policy: fire if no successful sync within one schedule cadence
# (plus slack). Catches the job silently stopping (scheduler issue, job
# deleted) rather than failing loudly. Skipped entirely for on-demand —
# there is no expected cadence to be absent from.
resource "google_monitoring_alert_policy" "sync_missing" {
  count        = local.create_scheduler ? 1 : 0
  display_name = "${var.job_name} sync overdue"
  project      = var.nonprod_project_id
  combiner     = "OR"

  conditions {
    display_name = "No successful sync in last ${local.absence_label}"

    condition_absent {
      filter   = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.sync_success.name}\" AND resource.type=\"cloud_run_job\""
      duration = "${local.absence_seconds}s" # derived from schedule cadence

      aggregations {
        alignment_period   = "3600s"
        per_series_aligner = "ALIGN_COUNT"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "The CloudSQL sync job **${var.job_name}** has not completed successfully in the last ${local.absence_label} (schedule: `${var.schedule}`). Check the scheduler and recent executions: https://console.cloud.google.com/run/jobs?project=${var.nonprod_project_id}"
    mime_type = "text/markdown"
  }

  depends_on = [google_logging_metric.sync_success]
}

# ── Secret Manager — nonprod DB password ─────────────────────────────────────
#
# After each restore the nonprod instance has prod's password. This secret
# stores the intended nonprod password. The sync job resets it after every
# restore so nonprod apps always connect with a stable, known credential.

resource "google_project_service" "secretmanager" {
  project            = var.nonprod_project_id
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

resource "google_secret_manager_secret" "nonprod_db_password" {
  secret_id = "${var.job_name}-nonprod-db-password"
  project   = var.nonprod_project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.secretmanager]
}

# Grant the job SA read access to the nonprod DB password secret.
resource "google_secret_manager_secret_iam_member" "job_secret_accessor" {
  project   = var.nonprod_project_id
  secret_id = google_secret_manager_secret.nonprod_db_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.job.email}"
}

# Note: the job SA's per-target roles/cloudsql.admin bindings
# (job_target_cloudsql_admin above) already include cloudsql.users.update,
# which is what reset_target_password needs — no extra binding required.
