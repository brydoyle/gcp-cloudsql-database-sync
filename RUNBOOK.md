# Runbook — CloudSQL Cross-Project Sync

**Service:** `cloudsql-sync` Cloud Run Job  
**Schedule:** Nightly at 02:00 UTC  
**Audience:** On-call engineers

---

## Quick Reference

```bash
# Trigger a manual sync
gcloud run jobs execute cloudsql-sync --region=us-central1 --project=NONPROD_PROJECT --wait

# Tail logs
gcloud logging read \
  'resource.type=cloud_run_job AND resource.labels.job_name=cloudsql-sync' \
  --project=NONPROD_PROJECT --limit=100 --order=asc

# Pause the nightly schedule
gcloud scheduler jobs pause cloudsql-sync-nightly --location=us-central1 --project=NONPROD_PROJECT

# Resume the nightly schedule
gcloud scheduler jobs resume cloudsql-sync-nightly --location=us-central1 --project=NONPROD_PROJECT

# List orphaned prod backups
gcloud sql backups list --instance=PROD_INSTANCE --project=PROD_PROJECT

# Delete a specific backup
gcloud sql backups delete BACKUP_ID --instance=PROD_INSTANCE --project=PROD_PROJECT
```

---

## Failure Scenarios

### 1. Job failed — exit code 1 (config error)

**Symptoms:** Logs contain `Missing required environment variable` or `not a valid GCP resource name`.

**Cause:** A Cloud Run Job environment variable is missing or malformed.

**Fix:**
```bash
# Inspect current env vars on the job
gcloud run jobs describe cloudsql-sync --region=us-central1 --project=NONPROD_PROJECT \
  --format="yaml(spec.template.spec.template.spec.containers[0].env)"

# Update a specific variable
gcloud run jobs update cloudsql-sync --region=us-central1 --project=NONPROD_PROJECT \
  --update-env-vars=VARIABLE_NAME=correct-value
```

---

### 2. Job failed — exit code 2 (API error)

**Symptoms:** Logs contain `HTTP 4xx` or `Operation ... failed`.

#### HTTP 403 — Permission denied

```bash
# Check what roles the job SA has on the prod project
gcloud projects get-iam-policy PROD_PROJECT \
  --flatten="bindings[].members" \
  --filter="bindings.members:cloudsql-sync@NONPROD_PROJECT.iam.gserviceaccount.com"

# Re-grant the missing role (cloudsql.admin or a custom role)
gcloud projects add-iam-policy-binding PROD_PROJECT \
  --member="serviceAccount:cloudsql-sync@NONPROD_PROJECT.iam.gserviceaccount.com" \
  --role="roles/cloudsql.admin"
```

#### HTTP 409 — Conflict (operation already in progress)

This means another backup, restore, or maintenance window was running when the job started.

```bash
# Check for in-progress operations on the prod instance
gcloud sql operations list --instance=PROD_INSTANCE --project=PROD_PROJECT \
  --filter="status!=DONE" --format="table(name,operationType,status,startTime)"

# Wait for it to complete, then trigger a manual run
gcloud run jobs execute cloudsql-sync --region=us-central1 --project=NONPROD_PROJECT --wait
```

#### HTTP 400 — Bad request (tier/version mismatch)

The nonprod instance must match or exceed prod in PostgreSQL version and machine tier.

```bash
# Compare instance specs
gcloud sql instances describe PROD_INSTANCE --project=PROD_PROJECT \
  --format="value(databaseVersion,settings.tier,settings.dataDiskSizeGb)"

gcloud sql instances describe NONPROD_INSTANCE --project=NONPROD_PROJECT \
  --format="value(databaseVersion,settings.tier,settings.dataDiskSizeGb)"
```

Upgrade the nonprod instance tier or disk if needed, then retry.

---

### 3. Job failed — exit code 3 (operation timed out)

**Symptoms:** Logs contain `did not complete within Xs`.

**Cause:** The backup or restore took longer than `OPERATION_TIMEOUT_SECONDS` (default: 7200s / 2 hours). Common with large databases.

**Fix:**
```bash
# Increase the timeout on the Cloud Run Job
gcloud run jobs update cloudsql-sync --region=us-central1 --project=NONPROD_PROJECT \
  --update-env-vars=OPERATION_TIMEOUT_SECONDS=14400

# Also extend the Cloud Run Job task timeout to match
gcloud run jobs update cloudsql-sync --region=us-central1 --project=NONPROD_PROJECT \
  --task-timeout=14400s
```

Check whether the operation eventually completed on its own:
```bash
gcloud sql operations list --instance=NONPROD_INSTANCE --project=NONPROD_PROJECT \
  --limit=5 --format="table(name,operationType,status,startTime,endTime)"
```

---

### 4. Orphaned backup in prod

**Symptoms:** The job completed (or crashed) but left an on-demand backup behind. The `delete_backup` step logs a warning rather than failing the job.

**Check:**
```bash
gcloud sql backups list --instance=PROD_INSTANCE --project=PROD_PROJECT \
  --format="table(id,status,type,startTime,endTime)"
```

On-demand backups created by this job have `type=ON_DEMAND`. Automated backups have `type=AUTOMATED`.

**Delete the orphaned backup:**
```bash
gcloud sql backups delete BACKUP_ID --instance=PROD_INSTANCE --project=PROD_PROJECT
```

---

### 5. Nonprod database unavailable after sync

**Cause:** The restore is still in progress, or it failed mid-restore leaving the instance in a partially restored state.

**Check restore status:**
```bash
gcloud sql operations list --instance=NONPROD_INSTANCE --project=NONPROD_PROJECT \
  --limit=3 --format="table(name,operationType,status,startTime,endTime)"
```

If the operation is `DONE` with an error, the instance may need to be restored from a different backup:
```bash
# List available prod backups
gcloud sql backups list --instance=PROD_INSTANCE --project=PROD_PROJECT \
  --format="table(id,status,type,startTime)"

# Manually trigger a restore from a specific backup ID
gcloud sql instances restore-backup NONPROD_INSTANCE \
  --backup-id=BACKUP_ID \
  --restore-instance=NONPROD_INSTANCE \
  --project=NONPROD_PROJECT \
  --backup-project=PROD_PROJECT \
  --backup-instance=PROD_INSTANCE
```

---

### 6. Scheduler fired during business hours / unexpected run

**Immediate action — pause the scheduler:**
```bash
gcloud scheduler jobs pause cloudsql-sync-nightly \
  --location=us-central1 --project=NONPROD_PROJECT
```

**If a job execution is already running, it cannot be stopped** — Cloud Run Jobs run to completion. Notify nonprod users and wait.

**Update the schedule if needed:**
```bash
gcloud scheduler jobs update http cloudsql-sync-nightly \
  --location=us-central1 \
  --schedule="0 2 * * *" \
  --time-zone="America/New_York" \
  --project=NONPROD_PROJECT
```

---

## Checking Job History

```bash
# Last 10 executions and their status
gcloud run jobs executions list --job=cloudsql-sync \
  --region=us-central1 --project=NONPROD_PROJECT \
  --limit=10 \
  --format="table(name,completionTime,status.conditions[0].type,status.conditions[0].status)"

# Logs for a specific execution
gcloud logging read \
  'resource.type=cloud_run_job AND resource.labels.job_name=cloudsql-sync AND resource.labels.execution_name=EXECUTION_NAME' \
  --project=NONPROD_PROJECT --order=asc \
  --format='table(timestamp,textPayload)'
```

---

## Escalation

If none of the above resolves the issue:

1. Check [Cloud SQL status page](https://status.cloud.google.com/) for regional incidents
2. Check Cloud Run status for the region
3. Escalate to the GCP-owning team with:
   - The execution name (from job history above)
   - Full log output
   - Output of `gcloud sql operations list` for both instances
