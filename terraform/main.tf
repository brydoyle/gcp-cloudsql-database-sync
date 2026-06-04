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

# Grant cloudsql.admin on the PROD project so the job can create/delete backups.
resource "google_project_iam_member" "job_prod_cloudsql_admin" {
  provider = google.prod
  project  = var.prod_project_id
  role     = "roles/cloudsql.admin"
  member   = "serviceAccount:${google_service_account.job.email}"
}

# Grant cloudsql.admin on the NONPROD project so the job can trigger restores.
resource "google_project_iam_member" "job_nonprod_cloudsql_admin" {
  project = var.nonprod_project_id
  role    = "roles/cloudsql.admin"
  member  = "serviceAccount:${google_service_account.job.email}"
}

# ── Scheduler service account ─────────────────────────────────────────────────

resource "google_service_account" "scheduler" {
  account_id   = "${var.job_name}-scheduler"
  display_name = "CloudSQL Sync Scheduler"
  project      = var.nonprod_project_id

  depends_on = [google_project_service.nonprod_apis]
}

resource "google_project_iam_member" "scheduler_run_invoker" {
  project = var.nonprod_project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.scheduler.email}"
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
        env {
          name  = "NONPROD_PROJECT_ID"
          value = var.nonprod_project_id
        }
        env {
          name  = "NONPROD_INSTANCE_NAME"
          value = var.nonprod_instance_name
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
          name = "NONPROD_DB_PASSWORD_SECRET"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.nonprod_db_password.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.nonprod_apis,
    google_project_iam_member.job_prod_cloudsql_admin,
    google_project_iam_member.job_nonprod_cloudsql_admin,
  ]
}

# ── Cloud Scheduler ───────────────────────────────────────────────────────────

resource "google_cloud_scheduler_job" "nightly" {
  name      = "${var.job_name}-nightly"
  location  = var.region
  project   = var.nonprod_project_id
  schedule  = var.schedule
  time_zone = var.timezone

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.nonprod_project_id}/jobs/${var.job_name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
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
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
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
      duration        = "0s"   # alert immediately on first occurrence
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
      period = "3600s"   # at most one alert per hour
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

# Alert policy: fire if no successful sync in the last 25 hours.
# Catches cases where the job silently stops running (scheduler issue,
# job deleted, etc.) rather than failing loudly.
resource "google_monitoring_alert_policy" "sync_missing" {
  display_name = "${var.job_name} sync not run in 25h"
  project      = var.nonprod_project_id
  combiner     = "OR"

  conditions {
    display_name = "No successful sync in last 25 hours"

    condition_absent {
      filter   = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.sync_success.name}\" AND resource.type=\"cloud_run_job\""
      duration = "90000s" # 25 hours — covers nightly schedule with 1h tolerance

      aggregations {
        alignment_period   = "3600s"
        per_series_aligner = "ALIGN_COUNT"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "The CloudSQL sync job **${var.job_name}** has not completed successfully in the last 25 hours. Check the scheduler and recent executions: https://console.cloud.google.com/run/jobs?project=${var.nonprod_project_id}"
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

# Grant the job SA permission to reset Cloud SQL users on nonprod.
resource "google_project_iam_member" "job_nonprod_cloudsql_user_admin" {
  project = var.nonprod_project_id
  role    = "roles/cloudsql.admin"  # already granted; listed here for documentation
  member  = "serviceAccount:${google_service_account.job.email}"

  # No-op if cloudsql.admin is already bound — kept for explicit dependency
  # tracking and to allow narrowing to a custom role later.
  lifecycle {
    ignore_changes = [role]
  }
}
