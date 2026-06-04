# Deploy Checklist — CloudSQL Cross-Project Sync

**Service:** `cloudsql-sync` Cloud Run Job  
**Deploy paths:** `sync_job/deploy.sh` (bash) or `terraform/` (Terraform)

---

## First-Time Deploy

### Pre-Deploy

- [ ] **Instance compatibility** — nonprod is the same PostgreSQL major version as prod, equal or larger tier:
  ```bash
  gcloud sql instances describe PROD_INSTANCE --project=PROD_PROJECT \
    --format="value(databaseVersion,settings.tier,settings.dataDiskSizeGb)"

  gcloud sql instances describe NONPROD_INSTANCE --project=NONPROD_PROJECT \
    --format="value(databaseVersion,settings.tier,settings.dataDiskSizeGb)"
  ```

- [ ] **Billing is paid** on the prod project — Free Trial blocks the backup API:
  ```bash
  gcloud billing projects describe PROD_PROJECT
  # billingEnabled: true  ← required
  ```

- [ ] **Backups can be created** on prod (smoke test):
  ```bash
  gcloud sql backups create --instance=PROD_INSTANCE --project=PROD_PROJECT
  ```

- [ ] **Tests pass:**
  ```bash
  pytest sync_job/test_main.py sync_job/test_configure.py -v
  ```

- [ ] **Nonprod users notified** of downtime window (restore takes ~7–30 min)

### Configure

```bash
cd sync_job
python3 configure.py
```

Verify `config.yaml` and `../terraform/terraform.tfvars` were written correctly.

### Deploy (choose one path)

**Bash:**
```bash
bash sync_job/deploy.sh
```

**Terraform:**
```bash
# Build image first
gcloud builds submit sync_job/ \
  --tag=gcr.io/NONPROD_PROJECT/cloudsql-sync \
  --project=NONPROD_PROJECT

cd terraform
terraform init
terraform plan   # review before applying
terraform apply
```

### Smoke Test

- [ ] **Trigger a manual run:**
  ```bash
  gcloud run jobs execute cloudsql-sync \
    --region=us-central1 \
    --project=NONPROD_PROJECT \
    --wait
  ```

- [ ] **Logs show success:**
  ```bash
  gcloud logging read \
    'resource.type=cloud_run_job AND resource.labels.job_name=cloudsql-sync' \
    --project=NONPROD_PROJECT \
    --order=desc --freshness=10m \
    --format='value(timestamp,textPayload)'
  ```
  Last line should be: `Sync finished successfully.`

- [ ] **No orphaned backups** left on prod:
  ```bash
  gcloud sql backups list --instance=PROD_INSTANCE --project=PROD_PROJECT \
    --filter="type=ON_DEMAND"
  ```

- [ ] **Scheduler confirmed** for next run:
  ```bash
  gcloud scheduler jobs describe cloudsql-sync-nightly \
    --location=us-central1 --project=NONPROD_PROJECT \
    --format="value(schedule,timeZone,scheduleTime)"
  ```

- [ ] **Alert email received** a test notification (or verify in Cloud Monitoring console)

---

## Re-Deploy (Code Update)

- [ ] Tests pass: `pytest sync_job/test_main.py sync_job/test_configure.py -v`
- [ ] **Bash:** `bash sync_job/deploy.sh` (idempotent)
- [ ] **Terraform:** `terraform plan && terraform apply`
- [ ] Trigger a manual smoke-test run

---

## Rollback Triggers

Roll back if any of the following occur after deployment:

| Trigger | Action |
|---|---|
| Job exits non-zero | Check logs → see RUNBOOK.md |
| Nonprod DB unavailable after job | See RUNBOOK.md §5 |
| Orphaned backup on prod | Delete manually — see RUNBOOK.md §4 |
| Scheduler firing at wrong time | Pause immediately: `gcloud scheduler jobs pause cloudsql-sync-nightly --location=us-central1 --project=NONPROD_PROJECT` |

---

## Rollback Procedure

**Bash path:**
```bash
# 1. Pause scheduler
gcloud scheduler jobs pause cloudsql-sync-nightly \
  --location=us-central1 --project=NONPROD_PROJECT

# 2. Redeploy previous image
gcloud run jobs update cloudsql-sync \
  --image=gcr.io/NONPROD_PROJECT/cloudsql-sync:PREVIOUS_TAG \
  --region=us-central1 --project=NONPROD_PROJECT

# 3. Resume once verified
gcloud scheduler jobs resume cloudsql-sync-nightly \
  --location=us-central1 --project=NONPROD_PROJECT
```

**Terraform path:**
```bash
git revert HEAD   # revert the bad change
terraform apply   # restore previous state
```
