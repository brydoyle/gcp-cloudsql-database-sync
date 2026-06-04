# Runbook — CloudSQL Cross-Project Sync

**Service:** `cloudsql-sync` Cloud Run Job  
**Schedule:** Nightly at 02:00 UTC (configurable)  
**Audience:** On-call engineers

---

## Quick Reference

```bash
# Trigger a manual sync
gcloud run jobs execute cloudsql-sync \
  --region=us-central1 --project=NONPROD_PROJECT --wait

# Live logs (refresh every 10s)
watch -n 10 "gcloud logging read \
  'resource.type=cloud_run_job AND resource.labels.job_name=cloudsql-sync' \
  --project=NONPROD_PROJECT --order=asc --freshness=5m \
  --format='value(timestamp,textPayload)' --limit=50"

# Last 5 execution results
gcloud run jobs executions list \
  --job=cloudsql-sync --region=us-central1 --project=NONPROD_PROJECT --limit=5

# Pause nightly schedule
gcloud scheduler jobs pause cloudsql-sync-nightly \
  --location=us-central1 --project=NONPROD_PROJECT

# Resume nightly schedule
gcloud scheduler jobs resume cloudsql-sync-nightly \
  --location=us-central1 --project=NONPROD_PROJECT

# List on-demand backups on prod
gcloud sql backups list --instance=PROD_INSTANCE --project=PROD_PROJECT \
  --filter="type=ON_DEMAND" --format="table(id,status,startTime,endTime)"

# Delete a specific backup
gcloud sql backups delete BACKUP_ID \
  --instance=PROD_INSTANCE --project=PROD_PROJECT
```

---

## Exit Codes

| Code | Meaning | Go to |
|---|---|---|
| `0` | Success | — |
| `1` | Config error (bad env vars, no credentials) | §1 |
| `2` | Cloud SQL API error | §2 |
| `3` | Operation timed out | §3 |
| `4` | Unexpected error (missing API field, unhandled exception) | Check logs |

---

## Failure Scenarios

### §1 — Exit code 1: Configuration error

**Symptoms:** Logs contain `Missing required environment variable` or `is not a valid GCP`.

**Cause:** A Cloud Run Job env var is missing, empty, or failed format validation.

**Fix:**
```bash
# Inspect current env vars
gcloud run jobs describe cloudsql-sync \
  --region=us-central1 --project=NONPROD_PROJECT \
  --format="yaml(spec.template.spec.template.spec.containers[0].env)"

# Fix by re-running configure and redeploying
cd sync_job && python3 configure.py && bash deploy.sh
```

---

### §2 — Exit code 2: Cloud SQL API error

Pull the specific error from logs first:
```bash
gcloud logging read \
  'resource.type=cloud_run_job AND resource.labels.job_name=cloudsql-sync' \
  --project=NONPROD_PROJECT --order=desc --freshness=30m \
  --format='value(timestamp,textPayload)' --limit=30
```

#### HTTP 400 — Bad request

Two possible causes:

**a) GCP Free Trial instance** — error text: `Operation is not allowed for Cloud SQL Free Trial Instance`
```bash
# Verify billing is enabled and paid
gcloud billing projects describe PROD_PROJECT
# Fix: upgrade billing at console.cloud.google.com/billing?project=PROD_PROJECT
# Then delete and recreate the instance to clear the Free Trial flag
```

**b) Tier or version mismatch** — nonprod is a smaller tier or different PostgreSQL version than prod
```bash
gcloud sql instances describe PROD_INSTANCE --project=PROD_PROJECT \
  --format="value(databaseVersion,settings.tier)"
gcloud sql instances describe NONPROD_INSTANCE --project=NONPROD_PROJECT \
  --format="value(databaseVersion,settings.tier)"
# Fix: upgrade the nonprod instance to match
```

#### HTTP 403 — Permission denied

```bash
# Check job SA bindings on prod
gcloud projects get-iam-policy PROD_PROJECT \
  --flatten="bindings[].members" \
  --filter="bindings.members:cloudsql-sync@NONPROD_PROJECT.iam.gserviceaccount.com"

# Re-grant if missing
gcloud projects add-iam-policy-binding PROD_PROJECT \
  --member="serviceAccount:cloudsql-sync@NONPROD_PROJECT.iam.gserviceaccount.com" \
  --role="roles/cloudsql.admin"
```

#### HTTP 404 — Not found

Instance name in config doesn't match what's in Cloud SQL.
```bash
gcloud sql instances list --project=PROD_PROJECT
gcloud sql instances list --project=NONPROD_PROJECT

# Fix: update config.yaml and redeploy
cd sync_job && python3 configure.py && bash deploy.sh
```

#### HTTP 409 — Conflict

Another operation is already in progress on the instance.
```bash
# Check for in-progress operations
gcloud sql operations list --instance=PROD_INSTANCE \
  --project=PROD_PROJECT --filter="status!=DONE" \
  --format="table(name,operationType,status,startTime)"

# Wait for it to finish, then re-trigger manually
gcloud run jobs execute cloudsql-sync \
  --region=us-central1 --project=NONPROD_PROJECT --wait
```

---

### §3 — Exit code 3: Operation timed out

**Symptoms:** Logs contain `did not complete within Xs`.

**Cause:** Backup or restore took longer than `OPERATION_TIMEOUT_SECONDS` (default 7200s). Common with large databases.

**Check if the operation eventually completed on its own:**
```bash
gcloud sql operations list --instance=NONPROD_INSTANCE \
  --project=NONPROD_PROJECT --limit=5 \
  --format="table(name,operationType,status,startTime,endTime)"
```

**Fix — increase the timeout:**
```bash
# Update config and redeploy
# Edit sync_job/config.yaml: OPERATION_TIMEOUT_SECONDS: 14400
bash sync_job/deploy.sh
```

Or via gcloud directly:
```bash
gcloud run jobs update cloudsql-sync \
  --region=us-central1 --project=NONPROD_PROJECT \
  --update-env-vars=OPERATION_TIMEOUT_SECONDS=14400 \
  --task-timeout=14400s
```

---

### §4 — Orphaned backup on prod

**Symptoms:** `delete_backup` step logged a warning but didn't fail the job. A backup with `type=ON_DEMAND` is left on the prod instance.

**Check:**
```bash
gcloud sql backups list --instance=PROD_INSTANCE --project=PROD_PROJECT \
  --format="table(id,status,type,startTime,endTime)"
```

**Delete:**
```bash
gcloud sql backups delete BACKUP_ID \
  --instance=PROD_INSTANCE --project=PROD_PROJECT
```

---

### §5 — Nonprod database unavailable after sync

**Check restore status:**
```bash
gcloud sql operations list --instance=NONPROD_INSTANCE \
  --project=NONPROD_PROJECT --limit=3 \
  --format="table(name,operationType,status,startTime,endTime)"
```

If the restore failed mid-way, manually restore from a different prod backup:
```bash
# List available prod backups
gcloud sql backups list --instance=PROD_INSTANCE --project=PROD_PROJECT \
  --format="table(id,status,type,startTime)"

# Manually restore
gcloud sql instances restore-backup NONPROD_INSTANCE \
  --backup-id=BACKUP_ID \
  --restore-instance=NONPROD_INSTANCE \
  --project=NONPROD_PROJECT \
  --backup-project=PROD_PROJECT \
  --backup-instance=PROD_INSTANCE
```

---

### §6 — Scheduler fired unexpectedly / wrong time

**Pause immediately:**
```bash
gcloud scheduler jobs pause cloudsql-sync-nightly \
  --location=us-central1 --project=NONPROD_PROJECT
```

Note: a running execution **cannot be stopped** — it will run to completion. Notify nonprod users.

**Update the schedule:**
```bash
# Edit sync_job/config.yaml: schedule: "0 3 * * *"
bash sync_job/deploy.sh
# or
terraform apply
```

---

## Checking execution history

```bash
# Last 10 executions with status
gcloud run jobs executions list \
  --job=cloudsql-sync --region=us-central1 --project=NONPROD_PROJECT \
  --limit=10 \
  --format="table(name,completionTime,status.conditions[0].type)"

# Logs for a specific execution
gcloud logging read \
  'resource.type=cloud_run_job AND resource.labels.execution_name=EXECUTION_NAME' \
  --project=NONPROD_PROJECT --order=asc \
  --format='value(timestamp,textPayload)'
```

---

## Escalation

If none of the above resolves the issue:

1. Check [Cloud SQL status](https://status.cloud.google.com/) for regional incidents
2. Check [Cloud Run status](https://status.cloud.google.com/) for the region
3. Escalate with:
   - Execution name (from history above)
   - Full log output
   - Output of `gcloud sql operations list` for both instances
   - Exit code from the failed execution
