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

# Parse config.yaml once — output all required values on separate lines,
# in a fixed order. This avoids spawning 8+ separate python3 processes.
_raw_config=$(python3 -c "
import sys
try:
    import yaml
    with open('${CONFIG_FILE}') as f:
        cfg = yaml.safe_load(f) or {}
except ImportError:
    cfg = {}
    with open('${CONFIG_FILE}') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and ':' in line:
                k, _, v = line.partition(':')
                cfg[k.strip()] = v.strip().strip(chr(39) + chr(34))

required = ['prod_project_id','prod_instance_name','nonprod_project_id',
            'nonprod_instance_name','region','schedule','timezone','job_name']
missing = [k for k in required if not cfg.get(k)]
if missing:
    for k in missing:
        print(f'ERROR: {k} is not set in config.yaml', file=sys.stderr)
    sys.exit(1)

# Output required keys then optional alert_email (may be empty)
for k in required + ['alert_email']:
    print(cfg.get(k, ''))
") || { log "ERROR: Failed to load config.yaml — run 'python3 configure.py' first."; exit 1; }

# Assign values by line position (matches the order printed above)
PROD_PROJECT=$(    sed -n '1p' <<< "${_raw_config}")
PROD_INSTANCE=$(   sed -n '2p' <<< "${_raw_config}")
NONPROD_PROJECT=$( sed -n '3p' <<< "${_raw_config}")
NONPROD_INSTANCE=$(sed -n '4p' <<< "${_raw_config}")
RUN_REGION=$(      sed -n '5p' <<< "${_raw_config}")
SCHEDULE=$(        sed -n '6p' <<< "${_raw_config}")
SCHEDULER_TIMEZONE=$(sed -n '7p' <<< "${_raw_config}")
JOB_NAME=$(        sed -n '8p' <<< "${_raw_config}")
ALERT_EMAIL=$(     sed -n '9p' <<< "${_raw_config}")

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
  # Filter by display name (label filtering is unreliable across gcloud versions).
  # Redirect stderr to /dev/null to prevent gcloud self-update noise from
  # contaminating the captured output.
  CHANNEL_NAME=$(gcloud beta monitoring channels list \
    --filter="displayName='CloudSQL Sync Alerts'" \
    --format="value(name)" \
    --project="${NONPROD_PROJECT}" 2>/dev/null | head -1)

  if [[ -z "${CHANNEL_NAME}" ]]; then
    CHANNEL_NAME=$(gcloud beta monitoring channels create \
      --display-name="CloudSQL Sync Alerts" \
      --type=email \
      --channel-labels="email_address=${ALERT_EMAIL}" \
      --format="value(name)" \
      --project="${NONPROD_PROJECT}" 2>/dev/null)
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
    # Write the policy as JSON — the gcloud CLI flag interface for threshold
    # conditions is unreliable across alpha/beta versions.
    POLICY_JSON=$(cat <<EOF
{
  "displayName": "${JOB_NAME} sync failed",
  "documentation": {
    "content": "The ${JOB_NAME} sync job failed. Check logs: https://console.cloud.google.com/run/jobs?project=${NONPROD_PROJECT}",
    "mimeType": "text/markdown"
  },
  "conditions": [{
    "displayName": "Sync job exited with non-zero code",
    "conditionThreshold": {
      "filter": "metric.type=\"logging.googleapis.com/user/${METRIC_NAME}\" AND resource.type=\"cloud_run_job\"",
      "comparison": "COMPARISON_GT",
      "thresholdValue": 0,
      "duration": "0s",
      "aggregations": [{
        "alignmentPeriod": "60s",
        "perSeriesAligner": "ALIGN_COUNT"
      }]
    }
  }],
  "alertStrategy": {
    "notificationRateLimit": { "period": "3600s" }
  },
  "combiner": "OR",
  "notificationChannels": ["${CHANNEL_NAME}"]
}
EOF
)
    echo "${POLICY_JSON}" | gcloud alpha monitoring policies create \
      --policy-from-file=- \
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
