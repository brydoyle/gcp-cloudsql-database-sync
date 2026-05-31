# CloudSQL Cross-Project Sync (Native Snapshots)

Nightly sync of a production PostgreSQL CloudSQL instance to a non-production instance
in a different GCP project using Cloud SQL's native backup/restore API.

## Architecture

```
Cloud Scheduler
      │ (nightly trigger)
      ▼
Cloud Run Job  (sync_job/main.py)
      │
      ├─1─▶ backupRuns.insert  (prod project)
      │         → on-demand snapshot of prod instance
      │
      └─2─▶ instances.restoreBackup  (nonprod project)
                ← cross-project reference to prod backup
```

No GCS bucket, no dump files — entirely managed by Cloud SQL.

---

## Files

| File | Purpose |
|---|---|
| `sync_job/main.py` | Cloud Run Job entrypoint |
| `sync_job/Dockerfile` | Container definition |
| `sync_job/requirements.txt` | Python dependencies |
| `sync_job/deploy.sh` | One-shot deploy script |

---

## Quick Start

1. **Edit `deploy.sh`** — fill in `PROD_PROJECT`, `PROD_INSTANCE`, `NONPROD_PROJECT`, `NONPROD_INSTANCE`.

2. **Run from `sync_job/`:**
   ```bash
   cd sync_job
   bash deploy.sh
   ```

3. **Test a manual run:**
   ```bash
   gcloud run jobs execute cloudsql-sync \
     --region=us-central1 \
     --project=your-nonprod-project-id
   ```

4. **Watch logs:**
   ```bash
   gcloud logging read \
     'resource.type=cloud_run_job AND resource.labels.job_name=cloudsql-sync' \
     --project=your-nonprod-project-id \
     --limit=50
   ```

---

## IAM Summary

The deploy script configures all of these automatically.

| Principal | Project | Role | Reason |
|---|---|---|---|
| Job SA | prod | `roles/cloudsql.admin` | Create, read, and delete on-demand backup |
| Job SA | nonprod | `roles/cloudsql.admin` | Trigger cross-project restore |
| Scheduler SA | nonprod | `roles/run.invoker` | Trigger Cloud Run Job |

For least-privilege, replace `roles/cloudsql.admin` with a custom role containing only:
- Prod: `cloudsql.backupRuns.create`, `cloudsql.backupRuns.get`, `cloudsql.backupRuns.delete`, `cloudsql.operations.get`
- Nonprod: `cloudsql.instances.restoreBackup`, `cloudsql.operations.get`

---

## Configuration

Environment variables set on the Cloud Run Job:

| Variable | Description |
|---|---|
| `PROD_PROJECT_ID` | GCP project containing the prod instance |
| `PROD_INSTANCE_NAME` | Cloud SQL instance name |
| `NONPROD_PROJECT_ID` | GCP project containing the non-prod instance |
| `NONPROD_INSTANCE_NAME` | Cloud SQL instance name |
| `POLL_INTERVAL_SECONDS` | Operation poll frequency (default: 15) |
| `OPERATION_TIMEOUT_SECONDS` | Max wait per operation (default: 7200) |

---

## Caveats

- **Destructive:** restore overwrites the entire non-prod instance. No rollback.
- **Downtime:** the non-prod instance is unavailable during the restore.
- **Instance tier must match:** the non-prod instance must have the same or larger machine type and disk size as prod, or the restore will fail.
- **Same major PostgreSQL version required** for cross-instance restore.
- On-demand backups created by this job count against your Cloud SQL backup retention quota.
