# GCP CloudSQL Cross-Project Sync

Nightly sync of a **production** PostgreSQL CloudSQL instance to a **non-production** instance in a different GCP project — using Cloud SQL's native backup/restore API. No GCS bucket, no dump files, no VMs.

> **Status:** POC — proven working. Production gaps (Private IP, Secret Manager) are documented in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## How it works

```
Cloud Scheduler  ──(cron, or on-demand)──▶  Cloud Run Job
                                                │
                                    ┌───────────┴───────────┐
                                    ▼                       ▼
                            prod project             nonprod project
                         backupRuns.insert      instances.restoreBackup
                         (create snapshot)      (restore from prod backup)
                                    │                       │
                                    └───────────┬───────────┘
                                                ▼
                                        backupRuns.delete
                                        (cleanup snapshot)
```

Total runtime: ~7–30 minutes depending on database size.

---

## Repository layout

```
.
├── sync_job/
│   ├── main.py              # Cloud Run Job — sync logic
│   ├── configure.py         # Interactive config wizard
│   ├── deploy.sh            # Bash deploy (reads config.yaml)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── config.yaml.example  # Config template (copy → config.yaml)
│   ├── test_main.py         # 94 unit tests for main.py
│   └── test_configure.py    # 61 unit tests for configure.py
├── terraform/
│   ├── main.tf              # All GCP resources
│   ├── variables.tf         # Inputs with validation
│   ├── outputs.tf           # Useful outputs + gcloud commands
│   ├── versions.tf          # Provider pins
│   └── terraform.tfvars.example
├── poc.sh                   # Rerunnable POC harness (up / test / down / status)
├── README.md                # This file
├── ARCHITECTURE.md          # Design decisions and trade-offs
├── DEPLOY_CHECKLIST.md      # Pre/post deployment verification
└── RUNBOOK.md               # Operational runbook for on-call
```

---

## Quick start

### Prerequisites

- `gcloud` CLI authenticated with Owner or sufficient IAM in both projects
- **The target instance(s) must already exist** — the job restores *into* them, it does not create them (see [Instance provisioning](#instance-provisioning) below)
- Both Cloud SQL instances must use the same **major PostgreSQL version**
- Non-prod instance must have equal or **larger** machine tier than prod
- Prod project must be on a **paid billing account** (Free Trial blocks backup API)

### Instance provisioning

The sync job **restores into existing instances** — it does **not** create them. This is deliberate: provisioning is declarative infrastructure (Terraform's job), while the sync is recurring data movement. Letting the job create instances would mix the two concerns and fight Terraform for ownership.

- **Target doesn't exist?** The job fails that target with `HTTP 404 — instance or backup not found`. Create the instance first.
- **Provisioning a new target:** use Terraform. See [`terraform/examples/target-instance.tf.example`](terraform/examples/target-instance.tf.example) for a ready-to-use instance resource (correct version/tier/edition matching + `prevent_destroy` guard).
- **Restore keeps the target's identity:** name, connection name, and IP are unchanged — only the data (and, via Secret Manager, the password) changes. Apps pointing at the target need no reconfiguration.

### Fastest path: the rerunnable POC harness

Once `configure.py` has been run, `poc.sh` stands up, exercises, and tears down the **entire** POC — instances included:

```bash
bash poc.sh up       # create both SQL instances, deploy, wire the password secret
bash poc.sh test     # run one sync; asserts restore + password reset + SQL verification
bash poc.sh down     # delete instances (disks & backups included), pause schedule → ~pennies/mo
bash poc.sh status   # what exists right now, and whether it's costing anything
```

Idempotent in both directions: `up` skips what exists, `down` ignores what's gone. The POC uses the smallest valid tier (`db-perf-optimized-N-2`) for **both** instances — the target only has to be ≥ prod, and a POC has no reason to pay for more.

### 1. Configure

```bash
cd sync_job
python3 configure.py
```

The wizard prompts for your project IDs, instance names, region, schedule, and alert email. It writes `config.yaml` (for the bash path) and `../terraform/terraform.tfvars` (for the Terraform path) in one step.

> **Pick one deploy path — not both.** `deploy.sh` and Terraform manage the *same* resources. Running both against one project causes ownership collisions. See [Choosing a deploy path](#choosing-a-deploy-path).

### 2a. Deploy with bash

```bash
bash deploy.sh
```

### 2b. Deploy with Terraform

```bash
# Build the container image first
gcloud builds submit sync_job/ \
  --tag=gcr.io/YOUR_NONPROD_PROJECT/cloudsql-sync \
  --project=YOUR_NONPROD_PROJECT

cd terraform
terraform init
terraform apply
```

### 3. Run a manual sync

```bash
gcloud run jobs execute cloudsql-sync \
  --region=us-central1 \
  --project=YOUR_NONPROD_PROJECT \
  --wait
```

### 4. Watch logs

```bash
gcloud logging read \
  'resource.type=cloud_run_job AND resource.labels.job_name=cloudsql-sync' \
  --project=YOUR_NONPROD_PROJECT \
  --order=desc \
  --freshness=10m \
  --format='value(timestamp,textPayload)'
```

---

## Multi-target restore

One prod backup can fan out to several non-prod targets in a single run (the backup is created once and each target is restored from it).

**Terraform:**
```hcl
nonprod_targets = [
  { project = "acme-dev", instance = "dev-db" },
  { project = "acme-qa",  instance = "qa-db" },
]
```

**Bash / manual** — set the `NONPROD_TARGETS` env var on the job (takes precedence over the single `NONPROD_PROJECT_ID`/`NONPROD_INSTANCE_NAME` pair):
```bash
gcloud run jobs update cloudsql-sync --region=us-central1 --project=YOUR_CONTROL_PROJECT \
  --update-env-vars='NONPROD_TARGETS=acme-dev:dev-db,acme-qa:qa-db'
```

A failure on one target does not block the others; the job exits non-zero at the end if any target failed, naming them. The job SA needs `cloudsql.admin` on each distinct target project (Terraform grants this automatically).

---

## Triggering a sync

The Cloud Run **Job** and Cloud **Scheduler** are decoupled. The scheduler is only the recurring trigger — the job can be run by anything with `run.invoker`. **The scheduler is optional**: choose `on-demand` in `configure.py` (or set `schedule: on-demand`) and no scheduler is created at all — the bash and Terraform paths both skip it, and an existing one is removed.

`configure.py` builds the schedule interactively — pick **weekly** (default: every Saturday night), **daily** or **weekdays** (it prompts for the time), **on-demand**, or a raw cron. No need to hand-write cron unless you want to.

| Trigger | How |
|---|---|
| **On-demand** | `gcloud run jobs execute cloudsql-sync --region=us-central1 --project=CONTROL_PROJECT --wait` |
| **Nightly (default)** | Cloud Scheduler → `jobs:run` on the configured cron |
| **CI/CD** | Same `gcloud run jobs execute` as a pipeline step |
| **Initial sync at provision time** | Optional — see below |

### Initial sync after provisioning

The job restores into existing instances, so a freshly-provisioned target starts empty. Two ways to populate it:

- **Recommended — run it as a separate action** after `terraform apply`:
  ```bash
  gcloud run jobs execute cloudsql-sync --region=us-central1 --project=CONTROL_PROJECT --wait
  ```
  Trivially scriptable in a Makefile or post-apply CI step. Keeps provisioning (state) and sync (action) cleanly separated.

- **Optional — sync at `terraform apply`** via a `local-exec` provisioner. See [`terraform/examples/initial-sync.tf.example`](terraform/examples/initial-sync.tf.example). Convenient for "day-0", but it couples `apply` to a 7–30 min data operation, needs `gcloud` on the apply host, and runs once on create (not idempotent infra). Read the caveats in the file before using it.

---

## Choosing a deploy path

`deploy.sh` and Terraform manage the **same** control-plane resources (service accounts, Cloud Run Job, scheduler, IAM, monitoring, secret). They are **mutually exclusive** — pick one owner per project.

| | `deploy.sh` | Terraform |
|---|---|---|
| Best for | Quick POCs, single-dev, throwaway envs | Shared / long-lived / prod-adjacent envs |
| State & drift | None — imperative | Tracked; `plan` shows drift |
| Change review | Diff the script | PR review on infra |
| Owns | Same resources | Same resources |

> The Cloud SQL **instances** are owned by neither — you create them separately (manually or via [`terraform/examples/target-instance.tf.example`](terraform/examples/target-instance.tf.example)). Only the control plane is contested.

### Built-in guard

To prevent accidentally running both, `deploy.sh` labels its Cloud Run Job `managed-by=deploy-sh`, and Terraform has a `check` block that **fails `plan`/`apply` with a clear message** if it finds a job carrying that label. (On a fresh project the check emits a one-time "job not found" warning — that's expected and harmless.)

### Migrating bash → Terraform (when a POC graduates)

The contested resources are cheap and stateless, so you have two safe options:

1. **Import** (no downtime, turnkey) — copy [`terraform/import.tf.example`](terraform/import.tf.example) to `import.tf`, fill in the placeholders, and `terraform plan && terraform apply`. The import blocks adopt the bash-created resources into state; the plan's remaining diff removes the `managed-by=deploy-sh` label (releasing the guard) and — with the default `least_privilege = true` — swaps the SA's `cloudsql.admin` for the narrow custom roles. Delete `import.tf` afterward.

2. **Tear down & re-apply** (brief control-plane gap, simplest) — delete the bash-created control-plane resources (the SQL instances are untouched), then `terraform apply` fresh. Re-set the Secret Manager value afterward.

The guard is now **symmetric**: Terraform refuses to touch a `deploy.sh`-labelled job, and `deploy.sh` refuses to touch a job without that label.

> **Upgrading from an older deploy.sh** (before labelling existed)? Your live job has no label, so the guard blocks it. Run `DEPLOY_SH_ADOPT=1 bash deploy.sh` once — it adopts the job and stamps the label; subsequent runs need no flag.

### Remote Terraform state

Local state is fine solo; for a team or CI, use the GCS backend: copy [`terraform/backend.tf.example`](terraform/backend.tf.example) to `backend.tf` (it includes the one-time bucket bootstrap commands) and run `terraform init -migrate-state`.

---

## Backup source

By default each run **creates a fresh on-demand backup** of prod, restores it, then **deletes it** (no quota accumulation). Alternatively, the job can **reuse the most recent existing backup** instead:

| `use_latest_existing_backup` | Behavior |
|---|---|
| `false` (default) | Create a new backup → restore → **delete it** afterward |
| `true` | Find the newest `SUCCESSFUL` backup (automated or on-demand) → restore from it → **leave it in place** |

When reusing, the job **never deletes** a backup it didn't create — only backups this job created are cleaned up. If no successful backup exists, the run fails with a clear message rather than silently creating one.

**Why use it:** faster (skips the 1–5 min backup step), avoids extra backup operations on prod, and lets you pin non-prod to an already-known-good nightly automated backup. **Trade-off:** non-prod reflects the backup's age, not "right now."

```hcl
# Terraform
use_latest_existing_backup = true
```
```bash
# Bash / manual
gcloud run jobs update cloudsql-sync --region=us-central1 --project=CONTROL_PROJECT \
  --update-env-vars=USE_LATEST_EXISTING_BACKUP=true
```

---

## Restore verification

By default (`verify_restore: true`) the job verifies each target after its restore, in two tiers:

1. **API-level (always):** `instances.get` must report the target `RUNNABLE` — catches restores that "completed" into a broken instance state.
2. **SQL-level (when the Secret Manager password reset is enabled):** the job opens a real connection via the Cloud SQL Python Connector as `postgres` with the freshly-reset password and runs `SELECT 1` — proving the engine serves queries *and* the credential reset took effect.

A verification failure marks that target failed (per-target isolation still applies; other targets proceed). Set `verify_restore: false` to skip.

---

## Configuration reference

All values are set via `config.yaml` (generated by `configure.py`). The wizard validates every field before saving.

| Field | Required | Default | Description |
|---|---|---|---|
| `prod_project_id` | ✅ | — | GCP project containing the prod Cloud SQL instance |
| `prod_instance_name` | ✅ | — | Cloud SQL instance name in prod |
| `nonprod_project_id` | ✅ | — | GCP project containing the non-prod instance |
| `nonprod_instance_name` | ✅ | — | Cloud SQL instance name in non-prod |
| `region` | ✅ | `us-central1` | GCP region for Cloud Run Job and Scheduler |
| `schedule` | ✅ | `0 23 * * 6` | 5-field cron, or `on-demand` for no scheduler. Default = every Saturday night 23:00. Built interactively by `configure.py`. |
| `timezone` | ✅ | `UTC` | Timezone for the schedule |
| `job_name` | ✅ | `cloudsql-sync` | Cloud Run Job name |
| `alert_email` | ☐ | — | Email to notify on failure (skips monitoring if blank) |
| `use_latest_existing_backup` | ☐ | `false` | Reuse the newest existing prod backup instead of creating one (see [Backup source](#backup-source)) |
| `verify_restore` | ☐ | `true` | Post-restore verification per target (see [Restore verification](#restore-verification)) |
| `vpc_connector` | ☐ | — | Serverless VPC Access connector for private egress (see [Networking](#networking)) |
| `vpc_network` / `vpc_subnetwork` | ☐ | — | Direct VPC egress (mutually exclusive with `vpc_connector`) |
| `vpc_egress` | ☐ | `private-ranges-only` | `private-ranges-only` or `all-traffic` |

The following are also set as environment variables on the Cloud Run Job:

| Variable | Default | Range |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `15` | 1–3600 |
| `OPERATION_TIMEOUT_SECONDS` | `7200` | 60–86400 |

---

## IAM

Both deploy paths configure all bindings automatically, but they differ in scope:

**Terraform (default: `least_privilege = true`)** grants the job SA narrow **custom roles** — nothing that can delete or reconfigure an instance:

| Principal | Project | Role | Permissions |
|---|---|---|---|
| Job SA | **prod** | custom `<job>_backup_ops` | `backupRuns.create/get/list/delete`, `operations.get`, `instances.get` |
| Job SA | **each target** | custom `<job>_restore_ops` | `instances.restoreBackup/get/connect`, `users.update/list`, `operations.get` |
| Scheduler SA | control | `roles/run.invoker` | Trigger the Cloud Run Job |

Set `least_privilege = false` to fall back to `roles/cloudsql.admin` (e.g. if a restore is denied a permission the custom roles miss — please file an issue if so).

**deploy.sh (POC path)** grants `roles/cloudsql.admin` on both projects — simpler, broader; another reason to graduate to Terraform for production.

---

## Monitoring & alerting

When `alert_email` is set, `deploy.sh` / Terraform creates:

1. A **log-based metric** counting executions where the container exits with a non-zero code
2. An **email notification channel**
3. A **failure alert** that fires immediately on any failed execution
4. An **overdue alert** (no success in 23.5h) — created **only for daily-or-finer schedules**: Cloud Monitoring alerting cannot look back more than 24h (hard platform limit), so weekly/monthly cadences rely on the failure alert instead

---

## Networking

The job's egress is **configurable so the tool works in any environment**:

| Mode | How to enable | Notes |
|---|---|---|
| **Public** (default) | Leave all `vpc_*` config blank | Zero prerequisites — but the deploy paths and the job itself **log a warning** on every run |
| **VPC connector** | `vpc_connector` | Serverless VPC Access connector (name or full resource ID) |
| **Direct VPC egress** | `vpc_network` (+ optional `vpc_subnetwork`) | No connector needed; the job gets an interface in your VPC |

`vpc_connector` and `vpc_network` are **mutually exclusive** — the wizard, `deploy.sh`, and Terraform all enforce this.

**Egress scope** (`vpc_egress`): `private-ranges-only` (default) sends only RFC-1918 traffic through the VPC — the job's Google API calls (sqladmin, Secret Manager) go directly and just work. `all-traffic` routes everything through the VPC — the subnet then needs **Private Google Access** (or Private Service Connect) enabled or the API calls will fail.

Because no VPC config is set by default, running publicly produces:
- a **deploy-time warning** from `deploy.sh`, a **plan-time warning** from Terraform (`check "public_networking"`), and
- a **runtime warning** in the job's logs on every execution (`SYNC_NETWORK_MODE` env var, stamped by both deploy paths).

Note this configures the *job's* egress. Giving the Cloud SQL **instances** private IPs is provisioned with the instances themselves — see the commented block in [`terraform/examples/target-instance.tf.example`](terraform/examples/target-instance.tf.example).

---

## Testing

```bash
# Install dependencies
pip install pytest google-api-python-client google-auth

# Run all 155 tests
pytest sync_job/test_main.py sync_job/test_configure.py -v
```

---

## Known limitations

| Limitation | Notes |
|---|---|
| **Destructive** | Restore overwrites the entire non-prod instance with no rollback |
| **Downtime** | Non-prod is unavailable during restore (~7–30 min) |
| **Version match required** | Non-prod must be the same major PostgreSQL version as prod |
| **Tier match required** | Non-prod must have equal or larger machine tier than prod |
| **Public egress by default** | Configurable — see [Networking](#networking). Public mode warns at deploy, plan, and runtime |
| **Manual password** | DB passwords are set manually. Production should use Secret Manager |

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design rationale and production path.

---

## Docs

| Document | Audience | Purpose |
|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Engineers | Design decisions, trade-offs, production path |
| [DEPLOY_CHECKLIST.md](DEPLOY_CHECKLIST.md) | Deployers | Pre/post deployment verification steps |
| [RUNBOOK.md](RUNBOOK.md) | On-call | Diagnosing and fixing failures |
