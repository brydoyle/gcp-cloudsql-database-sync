#!/usr/bin/env bash
# Deploy the CloudSQL snapshot sync job to Cloud Run Jobs + Cloud Scheduler.
# Run from the sync_job/ directory. Safe to re-run (idempotent).
#
# Prerequisites:
#   - gcloud CLI authenticated with sufficient IAM in both projects
#   - config.yaml present (run: python3 configure.py)
#
# Usage:
#   python3 configure.py   # first-time setup
#   bash deploy.sh         # deploy

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/config.yaml"

# ── Load config.yaml ─────────────────────────────────────────────────────────
if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "ERROR: config.yaml not found. Run 'python3 configure.py' first."
  exit 1
fi

log() { echo "[deploy] $*"; }

read_config() {
  python3 -c "
import sys
try:
    import yaml
    with open('${CONFIG_FILE}') as f:
        cfg = yaml.safe_load(f)
except ImportError:
    cfg = {}
    with open('${CONFIG_FILE}') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and ':' in line:
                k, _, v = line.partition(':')
                cfg[k.strip()] = v.strip().strip('\"')
key = sys.argv[1]
val = cfg.get(key, '')
if not val:
    print(f'ERROR: {key} is not set in config.yaml', file=sys.stderr)
    sys.exit(1)
print(val)
" "$1"
}

PROD_PROJECT=$(read_config prod_project_id)
PROD_INSTANCE=$(read_config prod_instance_name)
NONPROD_PROJECT=$(read_config nonprod_project_id)
NONPROD_INSTANCE=$(read_config nonprod_instance_name)
RUN_REGION=$(read_config region)
SCHEDULE=$(read_config schedule)
SCHEDULER_TIMEZONE=$(read_config timezone)
JOB_NAME=$(read_config job_name)

IMAGE="gcr.io/${NONPROD_PROJECT}/${JOB_NAME}"
JOB_SA="${JOB_NAME}@${NONPROD_PROJECT}.iam.gserviceaccount.com"
SCHEDULER_SA="${JOB_NAME}-scheduler@${NONPROD_PROJECT}.iam.gserviceaccount.com"

log "Loaded config:"
log "  Prod:    ${PROD_PROJECT} / ${PROD_INSTANCE}"
log "  Nonprod: ${NONPROD_PROJECT} / ${NONPROD_INSTANCE}"
log "  Region:  ${RUN_REGION}"
log "  Schedule: ${SCHEDULE} (${SCHEDULER_TIMEZONE})"

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

log "Waiting for service account to propagate..."
for i in $(seq 1 10); do
  if gcloud iam service-accounts describe "${JOB_SA}" --project="${NONPROD_PROJECT}" &>/dev/null; then
    log "Service account ready."
    break
  fi
  if [ "$i" -eq 10 ]; then
    log "ERROR: Service account ${JOB_SA} did not become available after 30s."
    exit 1
  fi
  log "  not ready yet, retrying in 3s... (attempt ${i}/10)"
  sleep 3
done

gcloud projects add-iam-policy-binding "${PROD_PROJECT}" \
  --member="serviceAccount:${JOB_SA}" \
  --role="roles/cloudsql.admin" \
  --condition=None

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
NONPROD_INSTANCE_NAME=${NONPROD_INSTANCE},\
GCP_REGION=${RUN_REGION}" \
  --project="${NONPROD_PROJECT}"

# ── 5. Cloud Scheduler ───────────────────────────────────────────────────────
log "Creating scheduler service account ${SCHEDULER_SA}..."
gcloud iam service-accounts create "${JOB_NAME}-scheduler" \
  --display-name="CloudSQL Sync Scheduler" \
  --project="${NONPROD_PROJECT}" 2>/dev/null || true

log "Waiting for scheduler service account to propagate..."
for i in $(seq 1 10); do
  if gcloud iam service-accounts describe "${SCHEDULER_SA}" --project="${NONPROD_PROJECT}" &>/dev/null; then
    log "Scheduler service account ready."
    break
  fi
  if [ "$i" -eq 10 ]; then
    log "ERROR: Service account ${SCHEDULER_SA} did not become available after 30s."
    exit 1
  fi
  log "  not ready yet, retrying in 3s... (attempt ${i}/10)"
  sleep 3
done

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

# ── 6. Monitoring & Alerting ──────────────────────────────────────────────────
ALERT_EMAIL=$(read_config alert_email 2>/dev/null || true)

if [[ -n "${ALERT_EMAIL}" ]]; then
  log "Enabling monitoring APIs..."
  gcloud services enable monitoring.googleapis.com logging.googleapis.com \
    --project="${NONPROD_PROJECT}"

  METRIC_NAME="${JOB_NAME}-failure"

  log "Creating log-based failure metric..."
  gcloud logging metrics create "${METRIC_NAME}" \
    --description="Failed executions of the ${JOB_NAME} Cloud Run Job" \
    --log-filter="resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${JOB_NAME}\" AND textPayload=~\"Container called exit\([1-9][0-9]*\)\"" \
    --project="${NONPROD_PROJECT}" 2>/dev/null || \
  gcloud logging metrics update "${METRIC_NAME}" \
    --description="Failed executions of the ${JOB_NAME} Cloud Run Job" \
    --log-filter="resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${JOB_NAME}\" AND textPayload=~\"Container called exit\([1-9][0-9]*\)\"" \
    --project="${NONPROD_PROJECT}"

  log "Creating email notification channel for ${ALERT_EMAIL}..."
  CHANNEL_NAME=$(gcloud monitoring channels list \
    --filter="type=email AND labels.email_address=${ALERT_EMAIL}" \
    --format="value(name)" \
    --project="${NONPROD_PROJECT}" | head -1)

  if [[ -z "${CHANNEL_NAME}" ]]; then
    CHANNEL_NAME=$(gcloud monitoring channels create \
      --display-name="CloudSQL Sync Alerts" \
      --type=email \
      --channel-labels="email_address=${ALERT_EMAIL}" \
      --format="value(name)" \
      --project="${NONPROD_PROJECT}")
    log "Created notification channel: ${CHANNEL_NAME}"
  else
    log "Reusing existing notification channel: ${CHANNEL_NAME}"
  fi

  log "Creating alert policy..."
  POLICY_EXISTS=$(gcloud alpha monitoring policies list \
    --filter="displayName='${JOB_NAME} sync failed'" \
    --format="value(name)" \
    --project="${NONPROD_PROJECT}" 2>/dev/null | head -1)

  if [[ -z "${POLICY_EXISTS}" ]]; then
    gcloud alpha monitoring policies create \
      --display-name="${JOB_NAME} sync failed" \
      --condition-display-name="Sync job exited with non-zero code" \
      --condition-filter="metric.type=\"logging.googleapis.com/user/${METRIC_NAME}\" AND resource.type=\"cloud_run_job\"" \
      --condition-threshold-value=0 \
      --condition-threshold-comparison=COMPARISON_GT \
      --condition-aggregations="alignmentPeriod=60s,perSeriesAligner=ALIGN_COUNT" \
      --duration=0s \
      --notification-channels="${CHANNEL_NAME}" \
      --documentation="The ${JOB_NAME} sync job failed. Check logs at https://console.cloud.google.com/run/jobs?project=${NONPROD_PROJECT}" \
      --project="${NONPROD_PROJECT}"
    log "Alert policy created — emails will be sent to ${ALERT_EMAIL} on failure."
  else
    log "Alert policy already exists: ${POLICY_EXISTS}"
  fi
else
  log "No alert_email set in config.yaml — skipping monitoring setup."
  log "  Add 'alert_email: you@example.com' to config.yaml and re-run to enable alerts."
fi

log ""
log "Done. Schedule: ${SCHEDULE} (${SCHEDULER_TIMEZONE})"
log ""
log "Manual run:"
log "  gcloud run jobs execute ${JOB_NAME} --region=${RUN_REGION} --project=${NONPROD_PROJECT}"
log ""
log "Logs:"
log "  gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=${JOB_NAME}' --project=${NONPROD_PROJECT} --limit=50"
