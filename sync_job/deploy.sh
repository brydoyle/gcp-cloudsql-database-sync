#!/usr/bin/env bash
# Deploy the CloudSQL snapshot sync job to Cloud Run Jobs + Cloud Scheduler.
# Run from the sync_job/ directory. Safe to re-run (idempotent).
#
# Prerequisites:
#   gcloud CLI authenticated with sufficient IAM in both projects.
#
# Usage:
#   Edit the variables below, then: bash deploy.sh

set -euo pipefail

# ── Edit these ──────────────────────────────────────────────────────────────
PROD_PROJECT="your-prod-project-id"
PROD_INSTANCE="your-prod-instance-name"

NONPROD_PROJECT="your-nonprod-project-id"
NONPROD_INSTANCE="your-nonprod-instance-name"

# Where to deploy the Cloud Run Job.
RUN_REGION="us-central1"
JOB_NAME="cloudsql-sync"
IMAGE="gcr.io/${NONPROD_PROJECT}/${JOB_NAME}"

# Cloud Scheduler — nightly at 02:00 UTC by default.
SCHEDULE="0 2 * * *"
SCHEDULER_TIMEZONE="UTC"
# ────────────────────────────────────────────────────────────────────────────

JOB_SA="${JOB_NAME}@${NONPROD_PROJECT}.iam.gserviceaccount.com"
SCHEDULER_SA="${JOB_NAME}-scheduler@${NONPROD_PROJECT}.iam.gserviceaccount.com"

log() { echo "[deploy] $*"; }

# ── 1. Enable required APIs ──────────────────────────────────────────────────
log "Enabling APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  sqladmin.googleapis.com \
  cloudbuild.googleapis.com \
  --project="${NONPROD_PROJECT}"

gcloud services enable sqladmin.googleapis.com --project="${PROD_PROJECT}"

# ── 2. Job service account ───────────────────────────────────────────────────
log "Creating job service account ${JOB_SA}..."
gcloud iam service-accounts create "${JOB_NAME}" \
  --display-name="CloudSQL Sync Job" \
  --project="${NONPROD_PROJECT}" 2>/dev/null || true

# Create backups on prod.
gcloud projects add-iam-policy-binding "${PROD_PROJECT}" \
  --member="serviceAccount:${JOB_SA}" \
  --role="roles/cloudsql.admin" \
  --condition=None

# Read prod backups + trigger restore on nonprod.
gcloud projects add-iam-policy-binding "${NONPROD_PROJECT}" \
  --member="serviceAccount:${JOB_SA}" \
  --role="roles/cloudsql.admin" \
  --condition=None

# ── 3. Build & push the container ────────────────────────────────────────────
log "Building container image ${IMAGE}..."
gcloud builds submit . \
  --tag="${IMAGE}" \
  --project="${NONPROD_PROJECT}"

# ── 4. Create/update the Cloud Run Job ───────────────────────────────────────
log "Deploying Cloud Run Job ${JOB_NAME}..."
gcloud run jobs deploy "${JOB_NAME}" \
  --image="${IMAGE}" \
  --region="${RUN_REGION}" \
  --service-account="${JOB_SA}" \
  --task-timeout="7200s" \
  --max-retries=1 \
  --set-env-vars="\
PROD_PROJECT_ID=${PROD_PROJECT},\
PROD_INSTANCE_NAME=${PROD_INSTANCE},\
NONPROD_PROJECT_ID=${NONPROD_PROJECT},\
NONPROD_INSTANCE_NAME=${NONPROD_INSTANCE}" \
  --project="${NONPROD_PROJECT}"

# ── 5. Cloud Scheduler ───────────────────────────────────────────────────────
log "Creating scheduler service account ${SCHEDULER_SA}..."
gcloud iam service-accounts create "${JOB_NAME}-scheduler" \
  --display-name="CloudSQL Sync Scheduler" \
  --project="${NONPROD_PROJECT}" 2>/dev/null || true

gcloud projects add-iam-policy-binding "${NONPROD_PROJECT}" \
  --member="serviceAccount:${SCHEDULER_SA}" \
  --role="roles/run.invoker" \
  --condition=None

JOB_URI="https://${RUN_REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${NONPROD_PROJECT}/jobs/${JOB_NAME}:run"

log "Creating/updating Cloud Scheduler job..."
gcloud scheduler jobs create http "${JOB_NAME}-nightly" \
  --location="${RUN_REGION}" \
  --schedule="${SCHEDULE}" \
  --time-zone="${SCHEDULER_TIMEZONE}" \
  --uri="${JOB_URI}" \
  --http-method=POST \
  --oauth-service-account-email="${SCHEDULER_SA}" \
  --project="${NONPROD_PROJECT}" 2>/dev/null || \
gcloud scheduler jobs update http "${JOB_NAME}-nightly" \
  --location="${RUN_REGION}" \
  --schedule="${SCHEDULE}" \
  --time-zone="${SCHEDULER_TIMEZONE}" \
  --uri="${JOB_URI}" \
  --http-method=POST \
  --oauth-service-account-email="${SCHEDULER_SA}" \
  --project="${NONPROD_PROJECT}"

log ""
log "Done. Schedule: ${SCHEDULE} ${SCHEDULER_TIMEZONE}"
log ""
log "Manual run:"
log "  gcloud run jobs execute ${JOB_NAME} --region=${RUN_REGION} --project=${NONPROD_PROJECT}"
log ""
log "Logs:"
log "  gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=${JOB_NAME}' --project=${NONPROD_PROJECT} --limit=50"
