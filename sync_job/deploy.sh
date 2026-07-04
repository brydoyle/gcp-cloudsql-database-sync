#!/usr/bin/env bash
# Deploy the CloudSQL snapshot sync job to Cloud Run Jobs + Cloud Scheduler.
# Run from the sync_job/ directory. Safe to re-run (idempotent).
#
# ⚠ This is the QUICKSTART/POC deploy path. For production, use terraform/
#   (state, drift detection, least-privilege IAM, PR-reviewed changes) — see
#   README "Choosing a deploy path". The two paths manage the same resources
#   and are mutually exclusive; this script refuses to touch a
#   Terraform-managed job, and Terraform refuses to touch a deploy.sh one.
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

# Output required keys, then optional keys (may be empty)
for k in required + ['alert_email', 'use_latest_existing_backup',
                     'vpc_connector', 'vpc_network', 'vpc_subnetwork', 'vpc_egress',
                     'verify_restore']:
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
USE_LATEST_BACKUP=$(sed -n '10p' <<< "${_raw_config}")
VPC_CONNECTOR=$(   sed -n '11p' <<< "${_raw_config}")
VPC_NETWORK=$(     sed -n '12p' <<< "${_raw_config}")
VPC_SUBNETWORK=$(  sed -n '13p' <<< "${_raw_config}")
VPC_EGRESS=$(      sed -n '14p' <<< "${_raw_config}")
VERIFY_RESTORE=$(  sed -n '15p' <<< "${_raw_config}")
# Defaults when unset/blank
USE_LATEST_BACKUP="${USE_LATEST_BACKUP:-false}"
VPC_EGRESS="${VPC_EGRESS:-private-ranges-only}"
VERIFY_RESTORE="${VERIFY_RESTORE:-true}"

# Optional private networking for the Cloud Run Job. Blank = public egress
# (works in any environment with no VPC prerequisites).
if [[ -n "${VPC_CONNECTOR}" && -n "${VPC_NETWORK}" ]]; then
  log "ERROR: vpc_connector and vpc_network are mutually exclusive — set one or the other."
  exit 1
fi
VPC_ARGS=()
NETWORK_MODE="public"
SYNC_NET="public"
if [[ -n "${VPC_CONNECTOR}" ]]; then
  VPC_ARGS+=("--vpc-connector=${VPC_CONNECTOR}" "--vpc-egress=${VPC_EGRESS}")
  NETWORK_MODE="vpc-connector (${VPC_CONNECTOR}, egress=${VPC_EGRESS})"
  SYNC_NET="private"
elif [[ -n "${VPC_NETWORK}" ]]; then
  VPC_ARGS+=("--network=${VPC_NETWORK}" "--vpc-egress=${VPC_EGRESS}")
  [[ -n "${VPC_SUBNETWORK}" ]] && VPC_ARGS+=("--subnet=${VPC_SUBNETWORK}")
  NETWORK_MODE="direct-vpc (${VPC_NETWORK}, egress=${VPC_EGRESS})"
  SYNC_NET="private"
fi
if [ "${SYNC_NET}" = "public" ]; then
  log "WARNING: job egress is PUBLIC (no VPC configured). Fine for a POC;"
  log "  for production set vpc_connector or vpc_network in config.yaml."
fi

IMAGE="gcr.io/${NONPROD_PROJECT}/${JOB_NAME}"
JOB_SA="${JOB_NAME}@${NONPROD_PROJECT}.iam.gserviceaccount.com"
SCHEDULER_SA="${JOB_NAME}-scheduler@${NONPROD_PROJECT}.iam.gserviceaccount.com"

# ── Guard: never overwrite a Terraform-managed job ───────────────────────────
# deploy.sh labels its job managed-by=deploy-sh. If the job exists WITHOUT
# that label, it belongs to Terraform (or something else) — refuse, mirroring
# Terraform's check "no_bash_deploy" in the other direction.
if EXISTING_OWNER=$(gcloud run jobs describe "${JOB_NAME}" \
    --region="${RUN_REGION}" --project="${NONPROD_PROJECT}" \
    --format="value(metadata.labels.managed-by)" 2>/dev/null); then
  if [ "${EXISTING_OWNER}" != "deploy-sh" ]; then
    log "ERROR: Cloud Run job '${JOB_NAME}' exists but is NOT managed by deploy.sh"
    log "  (label managed-by='${EXISTING_OWNER:-<none>}'). It is likely Terraform-managed."
    log "  Use 'terraform apply' for this project, or delete the job first if you"
    log "  intend to hand ownership back to deploy.sh. See README 'Choosing a deploy path'."
    exit 1
  fi
fi

log "Loaded config:"
log "  Prod:    ${PROD_PROJECT} / ${PROD_INSTANCE}"
log "  Nonprod: ${NONPROD_PROJECT} / ${NONPROD_INSTANCE}"
log "  Region:  ${RUN_REGION}"
log "  Schedule: ${SCHEDULE} (${SCHEDULER_TIMEZONE})"
log "  Backup:  $([ "${USE_LATEST_BACKUP}" = "true" ] && echo 'reuse latest existing' || echo 'create new')"
log "  Network: ${NETWORK_MODE}"

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
  --labels="managed-by=deploy-sh" \
  --set-env-vars="\
PROD_PROJECT_ID=${PROD_PROJECT},\
PROD_INSTANCE_NAME=${PROD_INSTANCE},\
NONPROD_PROJECT_ID=${NONPROD_PROJECT},\
NONPROD_INSTANCE_NAME=${NONPROD_INSTANCE},\
GCP_REGION=${RUN_REGION},\
USE_LATEST_EXISTING_BACKUP=${USE_LATEST_BACKUP},\
SYNC_NETWORK_MODE=${SYNC_NET},\
VERIFY_RESTORE=${VERIFY_RESTORE}" \
  ${VPC_ARGS[@]+"${VPC_ARGS[@]}"} \
  --project="${NONPROD_PROJECT}"

# ── 5. Cloud Scheduler ───────────────────────────────────────────────────────
# On-demand mode: no scheduler. Remove any existing one (so switching an
# already-scheduled job to on-demand actually takes effect) and skip the rest.
if [ "${SCHEDULE}" = "on-demand" ]; then
  log "Schedule is on-demand — no Cloud Scheduler will be created."
  gcloud scheduler jobs delete "${JOB_NAME}-nightly" \
    --location="${RUN_REGION}" --project="${NONPROD_PROJECT}" --quiet 2>/dev/null \
    && log "Removed existing scheduler job (now on-demand)." || true
else
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
fi

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

  log "Creating failure alert policy..."
  POLICY_EXISTS=$(gcloud alpha monitoring policies list \
    --filter="displayName='${JOB_NAME} sync failed'" \
    --format="value(name)" \
    --project="${NONPROD_PROJECT}" 2>/dev/null | head -1)

  if [[ -z "${POLICY_EXISTS}" ]]; then
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
      "aggregations": [{"alignmentPeriod": "60s", "perSeriesAligner": "ALIGN_COUNT"}]
    }
  }],
  "alertStrategy": { "notificationRateLimit": { "period": "3600s" } },
  "combiner": "OR",
  "notificationChannels": ["${CHANNEL_NAME}"]
}
EOF
)
    echo "${POLICY_JSON}" | gcloud alpha monitoring policies create \
      --policy-from-file=- --project="${NONPROD_PROJECT}"
    log "Failure alert policy created."
  else
    log "Failure alert policy already exists."
  fi

  # Success metric — fires when "Sync finished successfully." is logged.
  SUCCESS_METRIC="${JOB_NAME}-success"
  log "Creating log-based success metric..."
  gcloud logging metrics create "${SUCCESS_METRIC}" \
    --description="Successful executions of the ${JOB_NAME} Cloud Run Job" \
    --log-filter="resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${JOB_NAME}\" AND textPayload=\"Sync finished successfully.\"" \
    --project="${NONPROD_PROJECT}" 2>/dev/null || \
  gcloud logging metrics update "${SUCCESS_METRIC}" \
    --description="Successful executions of the ${JOB_NAME} Cloud Run Job" \
    --log-filter="resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${JOB_NAME}\" AND textPayload=\"Sync finished successfully.\"" \
    --project="${NONPROD_PROJECT}"

  # Absence alert — fires if no successful sync within one schedule cadence
  # (plus slack). Window is derived from the schedule so a weekly schedule
  # doesn't page daily. Skipped entirely for on-demand: no cadence to miss.
  # Remove the legacy fixed-25h policy from older deploys if present.
  STALE_POLICY=$(gcloud alpha monitoring policies list \
    --filter="displayName='${JOB_NAME} sync not run in 25h'" \
    --format="value(name)" \
    --project="${NONPROD_PROJECT}" 2>/dev/null | head -1)
  if [[ -n "${STALE_POLICY}" ]]; then
    gcloud alpha monitoring policies delete "${STALE_POLICY}" --quiet \
      --project="${NONPROD_PROJECT}" 2>/dev/null \
      && log "Removed legacy fixed-25h absence policy." || true
  fi

  if [ "${SCHEDULE}" = "on-demand" ]; then
    log "Schedule is on-demand — skipping the absence alert (no cadence to miss)."
  else
    # Derive the window from the cron's day-of-month / day-of-week fields.
    CRON_DOM=$(echo "${SCHEDULE}" | awk '{print $3}')
    CRON_DOW=$(echo "${SCHEDULE}" | awk '{print $5}')
    if [ "${CRON_DOW}" = "*" ] && [ "${CRON_DOM}" = "*" ]; then
      ABSENCE_SECONDS=90000;   ABSENCE_LABEL="25 hours"    # daily or finer
    elif [ "${CRON_DOW}" = "1-5" ]; then
      ABSENCE_SECONDS=262800;  ABSENCE_LABEL="73 hours"    # weekdays (weekend gap)
    elif [ "${CRON_DOM}" != "*" ]; then
      ABSENCE_SECONDS=2116800; ABSENCE_LABEL="24.5 days"   # monthly (API max)
    else
      ABSENCE_SECONDS=691200;  ABSENCE_LABEL="8 days"      # weekly
    fi

    ABSENCE_POLICY_EXISTS=$(gcloud alpha monitoring policies list \
      --filter="displayName='${JOB_NAME} sync overdue'" \
      --format="value(name)" \
      --project="${NONPROD_PROJECT}" 2>/dev/null | head -1)

    if [[ -z "${ABSENCE_POLICY_EXISTS}" ]]; then
      ABSENCE_JSON=$(cat <<EOF
{
  "displayName": "${JOB_NAME} sync overdue",
  "documentation": {
    "content": "The ${JOB_NAME} sync has not completed successfully in ${ABSENCE_LABEL} (schedule: ${SCHEDULE}). Check the scheduler: https://console.cloud.google.com/run/jobs?project=${NONPROD_PROJECT}",
    "mimeType": "text/markdown"
  },
  "conditions": [{
    "displayName": "No successful sync in last ${ABSENCE_LABEL}",
    "conditionAbsent": {
      "filter": "metric.type=\"logging.googleapis.com/user/${SUCCESS_METRIC}\" AND resource.type=\"cloud_run_job\"",
      "duration": "${ABSENCE_SECONDS}s",
      "aggregations": [{"alignmentPeriod": "3600s", "perSeriesAligner": "ALIGN_COUNT"}]
    }
  }],
  "combiner": "OR",
  "notificationChannels": ["${CHANNEL_NAME}"]
}
EOF
)
      echo "${ABSENCE_JSON}" | gcloud alpha monitoring policies create \
        --policy-from-file=- --project="${NONPROD_PROJECT}"
      log "Absence alert policy created (fires if no sync in ${ABSENCE_LABEL})."
    else
      log "Absence alert policy already exists."
      log "  NOTE: if you changed the schedule cadence, delete it and re-run to refresh the window:"
      log "  gcloud alpha monitoring policies delete ${ABSENCE_POLICY_EXISTS} --project=${NONPROD_PROJECT}"
    fi
  fi

else
  log "No alert_email set in config.yaml — skipping monitoring setup."
  log "  Add 'alert_email: you@example.com' to config.yaml and re-run to enable alerts."
fi

# ── 7. Secret Manager — nonprod DB password ───────────────────────────────────
SECRET_NAME="${JOB_NAME}-nonprod-db-password"
SECRET_EXISTS=$(gcloud secrets describe "${SECRET_NAME}" \
  --project="${NONPROD_PROJECT}" --format="value(name)" 2>/dev/null || true)

if [[ -z "${SECRET_EXISTS}" ]]; then
  log "Creating Secret Manager secret for nonprod DB password..."
  gcloud services enable secretmanager.googleapis.com --project="${NONPROD_PROJECT}"
  gcloud secrets create "${SECRET_NAME}" \
    --replication-policy=automatic \
    --project="${NONPROD_PROJECT}"

  # Grant the job SA read access to the secret.
  gcloud secrets add-iam-policy-binding "${SECRET_NAME}" \
    --member="serviceAccount:${JOB_SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --project="${NONPROD_PROJECT}"

  log ""
  log "⚠️  Secret created but has no value yet. Set the nonprod postgres password:"
  log "  echo -n 'YOUR_NONPROD_PASSWORD' | gcloud secrets versions add ${SECRET_NAME} --data-file=- --project=${NONPROD_PROJECT}"
  log ""
  log "Then update the Cloud Run Job to enable password reset after each sync:"
  log "  gcloud run jobs update ${JOB_NAME} --region=${RUN_REGION} --project=${NONPROD_PROJECT} \\"
  log "    --update-env-vars=NONPROD_DB_PASSWORD_SECRET=projects/${NONPROD_PROJECT}/secrets/${SECRET_NAME}"
else
  log "Secret Manager secret ${SECRET_NAME} already exists."
fi

log ""
log "Done. Schedule: ${SCHEDULE} (${SCHEDULER_TIMEZONE})"
log ""
log "Manual run:"
log "  gcloud run jobs execute ${JOB_NAME} --region=${RUN_REGION} --project=${NONPROD_PROJECT}"
log ""
log "Logs:"
log "  gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=${JOB_NAME}' --project=${NONPROD_PROJECT} --limit=50"
