output "job_name" {
  description = "Cloud Run Job name."
  value       = google_cloud_run_v2_job.sync.name
}

output "job_service_account" {
  description = "Email of the job service account."
  value       = google_service_account.job.email
}

output "scheduler_service_account" {
  description = "Email of the scheduler service account."
  value       = google_service_account.scheduler.email
}

output "scheduler_job_name" {
  description = "Cloud Scheduler job name."
  value       = google_cloud_scheduler_job.nightly.name
}

output "alert_policy_name" {
  description = "Cloud Monitoring alert policy name."
  value       = google_monitoring_alert_policy.sync_failure.name
}

output "manual_run_command" {
  description = "gcloud command to trigger a manual sync."
  value       = "gcloud run jobs execute ${var.job_name} --region=${var.region} --project=${var.nonprod_project_id} --wait"
}

output "logs_command" {
  description = "gcloud command to tail sync logs."
  value       = "gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=${var.job_name}' --project=${var.nonprod_project_id} --limit=50 --order=desc"
}

output "nonprod_db_password_secret" {
  description = "Secret Manager resource name for the nonprod DB password. Set the initial value with: gcloud secrets versions add JOB_NAME-nonprod-db-password --data-file=-"
  value       = google_secret_manager_secret.nonprod_db_password.name
}

output "set_nonprod_password_command" {
  description = "Command to set the initial nonprod DB password in Secret Manager."
  value       = "echo -n 'YOUR_NONPROD_PASSWORD' | gcloud secrets versions add ${google_secret_manager_secret.nonprod_db_password.secret_id} --data-file=- --project=${var.nonprod_project_id}"
}
