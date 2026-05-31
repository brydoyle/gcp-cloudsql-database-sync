# Deploy Checklist — CloudSQL Cross-Project Sync

**Service:** `cloudsql-sync` Cloud Run Job  
**Projects:** prod → nonprod  
**Region:** `us-central1` (update if different)

---

## First-Time Deploy

### Pre-Deploy

- [ ] **Variables set** — all six variables at the top of `deploy.sh` are filled in correctly
- [ ] **Credentials** — `gcloud auth list` shows the correct account; it has Owner or sufficient IAM in both projects
- [ ] **APIs enabled** — confirm `sqladmin.googleapis.com`, `run.googleapis.com`, `cloudscheduler.googleapis.com` are enabled in both projects:
  ```bash
  gcloud services list --project=YOUR_PROD_PROJECT | grep sqladmin
  gcloud services list --project=YOUR_NONPROD_PROJECT | grep -E "run|scheduler|sqladmin"
  ```
- [ ] **Instance compatibility** — nonprod instance is the same PostgreSQL major version as prod and has equal or larger disk/machine tier:
  ```bash
  gcloud sql instances describe YOUR_PROD_INSTANCE --project=YOUR_PROD_PROJECT --format="value(databaseVersion,settings.tier,settings.dataDiskSizeGb)"
  gcloud sql instances describe YOUR_NONPROD_INSTANCE --project=YOUR_NONPROD_PROJECT --format="value(databaseVersion,settings.tier,settings.dataDiskSizeGb)"
  ```
- [ ] **Nonprod instance accepts restores** — no active connections that will block the restore (warn users of downtime)
- [ ] **Tests pass locally:**
  ```bash
  cd sync_job && pip install -r requirements.txt pytest pytest-cov
  pytest test_main.py -v
  ```

### Deploy

- [ ] Run the deploy script:
  ```bash
  cd sync_job && bash deploy.sh
  ```
- [ ] Verify the Cloud Run Job was created:
  ```bash
  gcloud run jobs describe cloudsql-sync --region=us-central1 --project=YOUR_NONPROD_PROJECT
  ```
- [ ] Verify IAM bindings on prod project:
  ```bash
  gcloud projects get-iam-policy YOUR_PROD_PROJECT --flatten="bindings[].members" \
    --filter="bindings.members:cloudsql-sync@YOUR_NONPROD_PROJECT.iam.gserviceaccount.com"
  ```
- [ ] Verify Cloud Scheduler job was created:
  ```bash
  gcloud scheduler jobs describe cloudsql-sync-nightly --location=us-central1 --project=YOUR_NONPROD_PROJECT
  ```

### Smoke Test (Manual Run)

- [ ] **Trigger a manual run:**
  ```bash
  gcloud run jobs execute cloudsql-sync --region=us-central1 --project=YOUR_NONPROD_PROJECT --wait
  ```
- [ ] **Watch logs in real time:**
  ```bash
  gcloud logging read \
    'resource.type=cloud_run_job AND resource.labels.job_name=cloudsql-sync' \
    --project=YOUR_NONPROD_PROJECT --limit=100 --order=asc \
    --format='table(timestamp,textPayload)'
  ```
- [ ] Logs show `Sync finished successfully.`
- [ ] Nonprod database contains prod data (spot-check a table row count or recent record)
- [ ] No on-demand backups left orphaned in the prod instance:
  ```bash
  gcloud sql backups list --instance=YOUR_PROD_INSTANCE --project=YOUR_PROD_PROJECT
  ```

### Post-Deploy

- [ ] Notify nonprod users of the sync schedule (they will experience downtime nightly)
- [ ] Confirm the scheduler will fire at the expected time:
  ```bash
  gcloud scheduler jobs describe cloudsql-sync-nightly --location=us-central1 \
    --project=YOUR_NONPROD_PROJECT --format="value(schedule,timeZone,lastAttemptTime)"
  ```
- [ ] Set up a log-based alert in Cloud Monitoring for job failures:
  ```
  Filter: resource.type="cloud_run_job"
          resource.labels.job_name="cloudsql-sync"
          textPayload=~"error|Error|ERROR"
  ```

---

## Re-Deploy (Code Update)

- [ ] Tests pass: `pytest sync_job/test_main.py -v`
- [ ] Run `bash deploy.sh` — the script is idempotent and safe to re-run
- [ ] Trigger a manual smoke-test run (see above)

---

## Rollback Triggers

Roll back (redeploy the previous image tag) if any of the following occur after deployment:

| Trigger | Action |
|---|---|
| Sync job exits with non-zero code | Check logs; see RUNBOOK.md |
| Nonprod DB is inaccessible after the job completes | Restore from a previous prod backup manually |
| Orphaned on-demand backup remains in prod after the job | Delete it manually (see RUNBOOK.md) |
| Scheduler fires unexpectedly during business hours | Pause the scheduler: `gcloud scheduler jobs pause cloudsql-sync-nightly --location=us-central1 --project=YOUR_NONPROD_PROJECT` |

---

## Rollback Procedure

```bash
# 1. Pause the scheduler to prevent further runs
gcloud scheduler jobs pause cloudsql-sync-nightly \
  --location=us-central1 --project=YOUR_NONPROD_PROJECT

# 2. Redeploy the previous known-good image
gcloud run jobs update cloudsql-sync \
  --image=gcr.io/YOUR_NONPROD_PROJECT/cloudsql-sync:PREVIOUS_TAG \
  --region=us-central1 --project=YOUR_NONPROD_PROJECT

# 3. Resume the scheduler once verified
gcloud scheduler jobs resume cloudsql-sync-nightly \
  --location=us-central1 --project=YOUR_NONPROD_PROJECT
```
