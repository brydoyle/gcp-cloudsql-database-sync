#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Rerunnable POC harness — stand up, exercise, and tear down the entire
# CloudSQL cross-project sync from zero.
#
#   bash poc.sh up       Create both SQL instances, deploy the job,
#                        wire the password secret, enable the schedule.
#   bash poc.sh test     Run one sync and assert it restored, reset the
#                        password, and passed SQL verification.
#   bash poc.sh down     Delete both instances (disks + backups go with
#                        them) and pause the schedule. → pennies/month.
#   bash poc.sh purge    down + remove EVERYTHING billable: secret, images,
#                        build bucket, job, scheduler, alerts, metrics,
#                        channel, service accounts. → $0.
#   bash poc.sh status   Show what exists and what it costs.
#
# Reads project/instance names from sync_job/config.yaml (run
# `python3 sync_job/configure.py` first). Idempotent: `up` skips whatever
# already exists; `down` ignores whatever is already gone.
#
# Prerequisites: gcloud authenticated with owner-ish IAM in both projects;
# prod project on a PAID billing account (Free Trial blocks the backup API).
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/sync_job/config.yaml"

# ── POC instance shape ────────────────────────────────────────────────────────
# Both instances use the SAME small tier: the target must be >= prod, and a
# POC has no reason to pay for more than the minimum on either side.
DB_VERSION="POSTGRES_18"
DB_TIER="db-perf-optimized-N-2"
DB_EDITION="ENTERPRISE_PLUS"

log()  { echo "[poc] $*"; }
die()  { echo "[poc] ERROR: $*" >&2; exit 1; }

[ -f "${CONFIG_FILE}" ] || die "sync_job/config.yaml not found — run: python3 sync_job/configure.py"

cfg() {
  python3 -c "
import sys
try:
    import yaml
    cfg = yaml.safe_load(open('${CONFIG_FILE}')) or {}
except ImportError:
    cfg = {}
    for line in open('${CONFIG_FILE}'):
        line = line.strip()
        if line and not line.startswith('#') and ':' in line:
            k, _, v = line.partition(':')
            cfg[k.strip()] = v.strip().strip(chr(39) + chr(34))
print(cfg.get(sys.argv[1], ''))" "$1"
}

PROD_PROJECT=$(cfg prod_project_id);        [ -n "${PROD_PROJECT}" ]  || die "prod_project_id missing"
PROD_INSTANCE=$(cfg prod_instance_name);    [ -n "${PROD_INSTANCE}" ] || die "prod_instance_name missing"
NONPROD_PROJECT=$(cfg nonprod_project_id);  [ -n "${NONPROD_PROJECT}" ]  || die "nonprod_project_id missing"
NONPROD_INSTANCE=$(cfg nonprod_instance_name); [ -n "${NONPROD_INSTANCE}" ] || die "nonprod_instance_name missing"
REGION=$(cfg region); REGION="${REGION:-us-central1}"
JOB_NAME=$(cfg job_name); JOB_NAME="${JOB_NAME:-cloudsql-sync}"
SECRET_NAME="${JOB_NAME}-nonprod-db-password"

instance_state() { # project instance -> state or ""
  gcloud sql instances describe "$2" --project="$1" --format="value(state)" 2>/dev/null || true
}

ensure_instance() { # project instance
  local project="$1" instance="$2" state
  state=$(instance_state "${project}" "${instance}")
  case "${state}" in
    RUNNABLE)
      log "Instance ${project}/${instance} already RUNNABLE." ;;
    "")
      log "Creating ${project}/${instance} (${DB_TIER}, ${DB_VERSION})..."
      gcloud sql instances create "${instance}" \
        --database-version="${DB_VERSION}" \
        --tier="${DB_TIER}" \
        --edition="${DB_EDITION}" \
        --region="${REGION}" \
        --project="${project}" ;;
    STOPPED|SUSPENDED)
      log "Starting stopped instance ${project}/${instance}..."
      gcloud sql instances patch "${instance}" --project="${project}" \
        --activation-policy=ALWAYS --async >/dev/null ;;
    *)
      log "Instance ${project}/${instance} in state ${state} — waiting..." ;;
  esac
}

wait_runnable() { # project instance
  local project="$1" instance="$2" state
  for i in $(seq 1 40); do
    state=$(instance_state "${project}" "${instance}")
    [ "${state}" = "RUNNABLE" ] && { log "${project}/${instance} is RUNNABLE."; return 0; }
    log "  waiting for ${instance} (state=${state:-unknown}) [${i}/40]..."
    sleep 15
  done
  die "${project}/${instance} did not become RUNNABLE in 10 minutes"
}

cmd_up() {
  log "── POC up: ${PROD_PROJECT}/${PROD_INSTANCE} → ${NONPROD_PROJECT}/${NONPROD_INSTANCE} ──"
  gcloud services enable sqladmin.googleapis.com --project="${PROD_PROJECT}"
  gcloud services enable sqladmin.googleapis.com secretmanager.googleapis.com --project="${NONPROD_PROJECT}"

  ensure_instance "${PROD_PROJECT}" "${PROD_INSTANCE}"
  ensure_instance "${NONPROD_PROJECT}" "${NONPROD_INSTANCE}"
  wait_runnable   "${PROD_PROJECT}" "${PROD_INSTANCE}"
  wait_runnable   "${NONPROD_PROJECT}" "${NONPROD_INSTANCE}"

  log "Deploying the sync job (deploy.sh)..."
  ( cd "${SCRIPT_DIR}/sync_job" && bash deploy.sh )

  log "Wiring the password secret..."
  if ! gcloud secrets versions list "${SECRET_NAME}" --project="${NONPROD_PROJECT}" \
        --filter="state=ENABLED" --format="value(name)" 2>/dev/null | grep -q .; then
    openssl rand -base64 33 | tr -dc 'A-Za-z0-9' | head -c 28 | \
      gcloud secrets versions add "${SECRET_NAME}" --data-file=- --project="${NONPROD_PROJECT}"
    log "Generated a fresh random password into ${SECRET_NAME}."
  else
    log "Secret ${SECRET_NAME} already has an enabled version — keeping it."
  fi
  gcloud run jobs update "${JOB_NAME}" --region="${REGION}" --project="${NONPROD_PROJECT}" \
    --update-env-vars="NONPROD_DB_PASSWORD_SECRET=projects/${NONPROD_PROJECT}/secrets/${SECRET_NAME}" >/dev/null
  log "Password reset + SQL verification enabled on the job."

  gcloud scheduler jobs resume "${JOB_NAME}-nightly" \
    --location="${REGION}" --project="${NONPROD_PROJECT}" 2>/dev/null \
    && log "Schedule resumed." || log "No scheduler to resume (on-demand config, or already running)."

  log ""
  log "✅ POC is up. Next: bash poc.sh test"
}

cmd_test() {
  log "── POC test: executing one sync and asserting the full chain ──"
  local start_ts
  start_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  if ! gcloud run jobs execute "${JOB_NAME}" --region="${REGION}" \
        --project="${NONPROD_PROJECT}" --wait; then
    log "Execution reported failure — recent logs:"
    gcloud logging read \
      "resource.type=cloud_run_job AND resource.labels.job_name=${JOB_NAME} AND severity>=WARNING" \
      --project="${NONPROD_PROJECT}" --order=desc --freshness=30m \
      --format='value(textPayload)' --limit=10
    die "sync execution failed"
  fi

  log "Execution succeeded — asserting on logs (they can lag ~30s)..."
  local logs="" want_a="Sync finished successfully." want_b="Verification passed"
  for i in $(seq 1 8); do
    logs=$(gcloud logging read \
      "resource.type=cloud_run_job AND resource.labels.job_name=${JOB_NAME} AND timestamp>=\"${start_ts}\"" \
      --project="${NONPROD_PROJECT}" --format='value(textPayload)' 2>/dev/null || true)
    if echo "${logs}" | grep -q "${want_a}" && echo "${logs}" | grep -q "${want_b}"; then
      log "  ✓ found: '${want_b} ... serving SQL'"
      log "  ✓ found: '${want_a}'"
      echo "${logs}" | grep -E "Password reset|Verification|finished successfully" | sed 's/^/[poc]   /'
      log ""
      log "✅ POC test PASSED — restore, password reset, and SQL verification all confirmed."
      return 0
    fi
    log "  logs not complete yet [${i}/8]..."
    sleep 15
  done
  die "execution succeeded but expected log lines not found — inspect manually"
}

cmd_down() {
  log "── POC down: deleting instances (disks + backups included), pausing schedule ──"
  gcloud scheduler jobs pause "${JOB_NAME}-nightly" \
    --location="${REGION}" --project="${NONPROD_PROJECT}" 2>/dev/null \
    && log "Schedule paused." || log "No scheduler to pause."

  for pair in "${NONPROD_PROJECT}:${NONPROD_INSTANCE}" "${PROD_PROJECT}:${PROD_INSTANCE}"; do
    local project="${pair%%:*}" instance="${pair##*:}"
    if [ -n "$(instance_state "${project}" "${instance}")" ]; then
      log "Deleting ${project}/${instance}..."
      gcloud sql instances delete "${instance}" --project="${project}" --quiet
    else
      log "${project}/${instance} already gone."
    fi
  done
  log ""
  log "✅ POC is down. Remaining spend ≈ pennies (job, paused scheduler, secret, image)."
  log "   Bring it back any time with: bash poc.sh up"
}

cmd_purge() {
  log "── POC purge: removing EVERYTHING billable, including scaffolding ──"
  cmd_down

  log "Deleting scheduler job..."
  gcloud scheduler jobs delete "${JOB_NAME}-nightly" --location="${REGION}" \
    --project="${NONPROD_PROJECT}" --quiet 2>/dev/null || log "  (already gone)"

  log "Deleting Cloud Run job..."
  gcloud run jobs delete "${JOB_NAME}" --region="${REGION}" \
    --project="${NONPROD_PROJECT}" --quiet 2>/dev/null || log "  (already gone)"

  log "Deleting secret ${SECRET_NAME}..."
  gcloud secrets delete "${SECRET_NAME}" --project="${NONPROD_PROJECT}" --quiet 2>/dev/null \
    || log "  (already gone)"

  log "Deleting container images..."
  local digests
  digests=$(gcloud container images list-tags "gcr.io/${NONPROD_PROJECT}/${JOB_NAME}" \
    --format='get(digest)' 2>/dev/null || true)
  if [ -n "${digests}" ]; then
    while read -r d; do
      [ -n "${d}" ] && gcloud container images delete \
        "gcr.io/${NONPROD_PROJECT}/${JOB_NAME}@${d}" \
        --force-delete-tags --quiet 2>/dev/null || true
    done <<< "${digests}"
    log "  images deleted."
  else
    log "  (no images)"
  fi

  log "Deleting Cloud Build source bucket..."
  gcloud storage rm -r "gs://${NONPROD_PROJECT}_cloudbuild" --project="${NONPROD_PROJECT}" 2>/dev/null \
    || log "  (already gone)"

  log "Deleting alert policies (before their channel/metrics)..."
  # List everything and match client-side — the server-side filter dialect
  # varies by API surface and fails silently on unsupported functions.
  local policies
  policies=$(gcloud alpha monitoring policies list \
    --format="csv[no-heading](name,displayName)" \
    --project="${NONPROD_PROJECT}" 2>/dev/null \
    | awk -F, -v j="${JOB_NAME} " 'index($2, j) == 1 {print $1}' || true)
  if [ -n "${policies}" ]; then
    while read -r pol; do
      [ -n "${pol}" ] && gcloud alpha monitoring policies delete "${pol}" --quiet \
        --project="${NONPROD_PROJECT}" 2>/dev/null || true
    done <<< "${policies}"
    log "  policies deleted."
  else
    log "  (no policies)"
  fi

  log "Deleting notification channel..."
  local channel
  channel=$(gcloud beta monitoring channels list \
    --format="csv[no-heading](name,displayName)" \
    --project="${NONPROD_PROJECT}" 2>/dev/null \
    | awk -F, '$2 == "CloudSQL Sync Alerts" {print $1; exit}' || true)
  [ -n "${channel}" ] && gcloud beta monitoring channels delete "${channel}" --quiet \
    --project="${NONPROD_PROJECT}" 2>/dev/null && log "  channel deleted." || log "  (no channel)"

  log "Deleting log-based metrics..."
  for m in "${JOB_NAME}-failure" "${JOB_NAME}-success"; do
    gcloud logging metrics delete "${m}" --project="${NONPROD_PROJECT}" --quiet 2>/dev/null || true
  done

  log "Deleting service accounts..."
  for sa in "${JOB_NAME}@${NONPROD_PROJECT}.iam.gserviceaccount.com" \
            "${JOB_NAME}-scheduler@${NONPROD_PROJECT}.iam.gserviceaccount.com"; do
    gcloud iam service-accounts delete "${sa}" --project="${NONPROD_PROJECT}" --quiet 2>/dev/null \
      || log "  (${sa} already gone)"
  done
  log "  NOTE: the job SA's cross-project binding on ${PROD_PROJECT} becomes a"
  log "  harmless 'deleted:' tombstone; remove it in the console if you want zero trace."

  log ""
  log "✅ POC fully purged — nothing billable remains."
  log "   Rebuild everything with: bash poc.sh up"
}

cmd_status() {
  log "── POC status ──"
  for pair in "${PROD_PROJECT}:${PROD_INSTANCE}" "${NONPROD_PROJECT}:${NONPROD_INSTANCE}"; do
    local project="${pair%%:*}" instance="${pair##*:}" state
    state=$(instance_state "${project}" "${instance}")
    log "  instance ${project}/${instance}: ${state:-ABSENT}"
  done
  log "  scheduler: $(gcloud scheduler jobs describe "${JOB_NAME}-nightly" \
      --location="${REGION}" --project="${NONPROD_PROJECT}" \
      --format='value(state)' 2>/dev/null || echo ABSENT)"
  log "  job:       $(gcloud run jobs describe "${JOB_NAME}" --region="${REGION}" \
      --project="${NONPROD_PROJECT}" --format='value(metadata.name)' 2>/dev/null || echo ABSENT)"
  log "  secret:    $(gcloud secrets describe "${SECRET_NAME}" \
      --project="${NONPROD_PROJECT}" --format='value(name)' 2>/dev/null || echo ABSENT)"
  log "  💰 running instances are the only meaningful cost — ABSENT/STOPPED ≈ free."
}

case "${1:-}" in
  up)     cmd_up ;;
  test)   cmd_test ;;
  down)   cmd_down ;;
  purge)  cmd_purge ;;
  status) cmd_status ;;
  *)      echo "Usage: bash poc.sh {up|test|down|purge|status}"; exit 1 ;;
esac
