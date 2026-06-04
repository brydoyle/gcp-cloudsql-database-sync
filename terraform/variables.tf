variable "prod_project_id" {
  description = "GCP project ID containing the production Cloud SQL instance."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9\\-]{4,28}[a-z0-9]$", var.prod_project_id))
    error_message = "prod_project_id must be 6–30 chars, start with a lowercase letter, letters/digits/hyphens only."
  }
}

variable "prod_instance_name" {
  description = "Name of the production Cloud SQL instance."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9\\-]{0,96}([a-z0-9])?$", var.prod_instance_name))
    error_message = "prod_instance_name must start with a lowercase letter, 1–98 chars, letters/digits/hyphens only."
  }
}

variable "nonprod_project_id" {
  description = "Control project: where the Cloud Run Job, service accounts, secret, and monitoring live. Also the default single restore target when nonprod_targets is empty."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9\\-]{4,28}[a-z0-9]$", var.nonprod_project_id))
    error_message = "nonprod_project_id must be 6–30 chars, start with a lowercase letter, letters/digits/hyphens only."
  }
}

variable "nonprod_instance_name" {
  description = "Default single non-production Cloud SQL instance (used when nonprod_targets is empty)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9\\-]{0,96}([a-z0-9])?$", var.nonprod_instance_name))
    error_message = "nonprod_instance_name must start with a lowercase letter, 1–98 chars, letters/digits/hyphens only."
  }
}

variable "nonprod_targets" {
  description = "Optional multi-target fan-out. List of restore destinations; if empty, the single nonprod_project_id/nonprod_instance_name pair is used. The job SA is granted cloudsql.admin on each distinct target project."
  type = list(object({
    project  = string
    instance = string
  }))
  default = []
}

variable "region" {
  description = "GCP region for Cloud Run Job and Cloud Scheduler."
  type        = string
  default     = "us-central1"
}

variable "job_name" {
  description = "Name of the Cloud Run Job."
  type        = string
  default     = "cloudsql-sync"

  validation {
    condition     = can(regex("^[a-z][a-z0-9\\-]{0,48}$", var.job_name))
    error_message = "job_name must start with a lowercase letter, 1–49 chars, letters/digits/hyphens only."
  }
}

variable "container_image" {
  description = "Full container image URI for the sync job. Build with: gcloud builds submit sync_job/ --tag=gcr.io/PROJECT/cloudsql-sync"
  type        = string
}

variable "schedule" {
  description = "Cron schedule for the nightly sync (default: 02:00 UTC)."
  type        = string
  default     = "0 2 * * *"
}

variable "timezone" {
  description = "Timezone for the Cloud Scheduler schedule."
  type        = string
  default     = "UTC"
}

variable "alert_email" {
  description = "Email address to notify when the sync job fails."
  type        = string
}

variable "task_timeout_seconds" {
  description = "Maximum duration for a single Cloud Run Job task (seconds)."
  type        = number
  default     = 7200

  validation {
    condition     = var.task_timeout_seconds >= 60 && var.task_timeout_seconds <= 86400
    error_message = "task_timeout_seconds must be between 60 and 86400."
  }
}

variable "poll_interval_seconds" {
  description = "How often the job polls Cloud SQL operation status (seconds)."
  type        = number
  default     = 15

  validation {
    condition     = var.poll_interval_seconds >= 1 && var.poll_interval_seconds <= 3600
    error_message = "poll_interval_seconds must be between 1 and 3600."
  }
}

variable "use_latest_existing_backup" {
  description = "If true, reuse the most recent existing backup of the prod instance instead of creating a new one. A reused backup is never deleted by the job. Default false (create a fresh backup each run)."
  type        = bool
  default     = false
}
