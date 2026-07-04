# Architecture — GCP CloudSQL Cross-Project Sync

## Context

Teams running production workloads on Cloud SQL PostgreSQL need non-production environments that reflect current production data. Manual database refreshes are error-prone, inconsistent, and create compliance risk if done ad-hoc.

This solution automates a scheduled (default: weekly, Saturday night) or on-demand one-way sync from prod to one or more non-prod targets across GCP project boundaries.

---

## Goals

- **Automated** — no human intervention for routine refreshes
- **Native GCP** — no external tools, no VMs, no GCS bucket
- **Cross-project** — prod and non-prod live in separate GCP projects (billing isolation, IAM isolation)
- **Observable** — structured logs, alert on failure
- **Safe** — guards against accidental prod-to-prod restore, Free Trial restrictions, and misconfigurations caught at startup

---

## High-level design

```
┌─────────────────────────────────────────────────────────────────┐
│  nonprod project                                                │
│                                                                 │
│  Cloud Scheduler ──(cron)──▶ Cloud Run Job                      │
│                               (cloudsql-sync SA)                │
│                                      │                          │
│          ┌───────────────────────────┼──────────────────────┐   │
│          │                           │                      │   │
│          ▼                           ▼                      ▼   │
│  ┌──────────────┐          ┌──────────────────┐   ┌────────────┐│
│  │ Cloud SQL    │          │  Cloud SQL       │   │ Cloud      ││
│  │ Admin API    │          │  Admin API       │   │ Monitoring ││
│  │ (prod proj)  │          │  (nonprod proj)  │   │ + Alerting ││
│  └──────────────┘          └──────────────────┘   └────────────┘│
│          │                           │                           │
└──────────┼───────────────────────────┼───────────────────────────┘
           │                           │
┌──────────▼──────────┐   ┌────────────▼────────────┐
│  prod project       │   │  nonprod project        │
│                     │   │                         │
│  Cloud SQL          │   │  Cloud SQL              │
│  (production-db)    │   │  (nonprod-db)           │
│                     │   │                         │
└─────────────────────┘   └─────────────────────────┘
```

### Sync flow (step by step)

| Step | API call | Description |
|---|---|---|
| 1 | `backupRuns.insert` (prod) — or `backupRuns.list` when `USE_LATEST_EXISTING_BACKUP` | Create an on-demand snapshot, **or** reuse the newest existing `SUCCESSFUL` backup |
| 2 | `operations.get` (prod) | Poll until backup is `DONE` (create mode only) |
| 3 | `instances.restoreBackup` (per target) | Restore using a cross-project backup reference — looped over every target |
| 4 | `operations.get` (per target) | Poll until each restore is `DONE` |
| 5 | `backupRuns.delete` (prod) | Delete the backup — **only if this job created it**; a reused backup is left in place |

Step 5 runs in a `finally` block — it always executes even if a restore fails, and its own failure is non-fatal (warns only) so a cleanup error never masks a successful sync. Ownership is explicit: `acquire_backup()` returns `(backup_id, created_by_us)` and cleanup only fires when `created_by_us` is true.

---

## Key design decisions

### Native backup/restore vs. pg_dump/restore

Native snapshot backup/restore was chosen. This is the single most consequential design decision and constrains what the tool can and cannot do — see **[ADR-001](#adr-001-snapshot-restore-vs-logical-dump)** below for the full analysis, including why table-level filtering and in-flight data masking are fundamentally incompatible with this approach.

### Cloud Run Job vs. Cloud Functions vs. GCE VM

| Option | Chosen | Reason |
|---|---|---|
| **Cloud Run Job** | ✅ | Serverless, scales to zero, built-in retry, max 24h timeout |
| Cloud Functions | ❌ | 9-minute max timeout — too short for large databases |
| GCE VM | ❌ | Always-on cost, requires OS patching, over-engineered for a periodic job |

### Error handling — exceptions vs. sys.exit()

The poller (`wait_for_operation`) raises typed exceptions (`SyncError`, `OperationTimeout`) rather than calling `sys.exit()`. This allows `delete_backup()` — which is non-fatal cleanup — to catch errors naturally without the `except SystemExit` antipattern. `sys.exit()` is only called at the top-level boundary in `__main__`.

### Config validation — fail fast, report all errors

All environment variables are validated at startup before any API call is made. The validator collects all errors and reports them together, so a misconfigured environment reports every problem in a single run rather than one at a time.

### Two deployment paths — bash and Terraform

| Path | When to use |
|---|---|
| `deploy.sh` | Quick deploys, POCs, single-developer setups |
| Terraform | Team environments, code review for infra changes, drift detection, state tracking |

Both paths read from `config.yaml` / `terraform.tfvars` generated by `configure.py`. The config wizard is the single source of truth for values.

---

## Component inventory

| Component | Project | Purpose |
|---|---|---|
| Cloud Run Job `cloudsql-sync` | nonprod | Runs the sync container |
| Cloud Scheduler `cloudsql-sync-nightly` | nonprod | Triggers the job on cron — **omitted when schedule is `on-demand`** |
| Service Account `cloudsql-sync@nonprod` | nonprod | Job identity; has `cloudsql.admin` on prod + each target project |
| Service Account `cloudsql-sync-scheduler@nonprod` | nonprod | Scheduler identity; `run.invoker` — omitted when on-demand |
| Log-based metric `cloudsql-sync-failure` | nonprod | Counts non-zero exit codes |
| Log-based metric `cloudsql-sync-success` | nonprod | Counts successful syncs (feeds the overdue alert) |
| Notification channel (email) | nonprod | Delivers alerts |
| Alert policy `cloudsql-sync sync failed` | nonprod | Fires on first failure, rate-limited to 1/hour |
| Alert policy `cloudsql-sync sync overdue` | nonprod | Fires if no success within one schedule cadence + slack (window derived from the cron) — omitted when on-demand |
| Secret `cloudsql-sync-nonprod-db-password` | nonprod | Optional post-restore password reset source |
| Container image `gcr.io/nonprod/cloudsql-sync` | nonprod | Built by Cloud Build from `sync_job/` |

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Configuration error (missing/invalid env vars, bad credentials) |
| `2` | Cloud SQL API error (HTTP 4xx/5xx, operation failed) |
| `3` | Operation timed out |
| `4` | Unexpected error (missing field in API response, unhandled exception) |

---

## Production gaps (POC → Production)

The following are known gaps from POC to a production-hardened deployment. They are out of scope for the POC but straightforward to close.

### 1. Networking — Public IP

**Current state:** Both Cloud SQL instances use public IP addresses.

**Production fix:** Use Private IP with VPC peering. The Cloud Run Job connects via a Serverless VPC Access connector. At corporate scale, the existing Dedicated Interconnect already provides private connectivity — route the VPC connector through the correct subnet.

**Terraform path:** Add `google_compute_network`, `google_vpc_access_connector`, and set `private_ip_address` on both SQL instances.

---

### 2. Secrets — Manual password management ✅ ADDRESSED

**Resolved.** The job now optionally resets each target's postgres password from **Secret Manager** after every restore (`reset_target_password`, opt-in via `NONPROD_DB_PASSWORD_SECRET`). Both deploy paths create the secret and grant the job SA `secretmanager.secretAccessor`. Remaining nice-to-have: a rotation policy on the secret.

---

### 3. Monitoring — Metrics only, no SLO ✅ ADDRESSED

**Resolved.** Added a log-based **success** metric plus a 25-hour **absence** alert (`sync_missing` / "sync not run in 25h") alongside the failure alert — this catches silent scheduler failures the failure alert would miss. Remaining nice-to-have: a restore-duration metric for latency trends.

---

### 4. Data masking (if required)

**Current state:** Prod data is copied as-is to nonprod.

**Production fix:** Fundamentally incompatible with snapshot restore — see [ADR-001](#adr-001-snapshot-restore-vs-logical-dump). Requires either post-restore SQL masking (leaves a PII exposure window) or switching to the logical-dump path.

---

## Multi-target restore

A single prod backup can fan out to **multiple** non-production targets (e.g. dev, qa, staging) in one run. Because the backup is created once and `instances.restoreBackup` simply references it, the marginal cost of an extra target is one more restore — no extra snapshot.

**How it's configured:**

| Path | Single target | Multi-target |
|---|---|---|
| Env var | `NONPROD_PROJECT_ID` + `NONPROD_INSTANCE_NAME` | `NONPROD_TARGETS="dev-proj:dev-db,qa-proj:qa-db"` |
| Terraform | `nonprod_instance_name` | `nonprod_targets = [{project=…, instance=…}, …]` |

`NONPROD_TARGETS` takes precedence over the single pair when both are set.

**Execution semantics:**
- One backup → loop restore over every target → one cleanup. The backup is deleted once, in a `finally`, after all targets are attempted.
- **Per-target isolation:** a failure on one target is logged and recorded but does **not** abort the others. After all targets are attempted, the job exits non-zero (exit 2) if any failed, naming the failed targets.
- The password reset (if enabled) runs per target with the same secret value, fetched once up front.

**Guards applied per target:** project/instance format validation, the prod≠target safety check, and a duplicate-target check. The job SA needs `cloudsql.admin` on **each** distinct target project — Terraform grants this automatically via `for_each` over the distinct target projects.

---

## ADR-001: Snapshot restore vs. logical dump

**Status:** Accepted · **Date:** 2026-06 · **Context:** POC, may be revisited if masking/filtering is required

### Context

The sync must copy a production Cloud SQL PostgreSQL database to non-production. Two mechanisms exist:

1. **Native snapshot backup/restore** — `backupRuns.insert` then `instances.restoreBackup`. Operates on the **whole instance** as an opaque block-level image.
2. **Logical dump/restore** — `pg_dump`/`pg_restore` or the Cloud SQL `export`/`import` API producing a SQL file in GCS.

### Decision

Use **native snapshot backup/restore**.

### Rationale

| Factor | Snapshot (chosen) | Logical dump |
|---|---|---|
| Infrastructure | None — no GCS bucket, no psql tooling in the container | GCS bucket + tooling + more IAM |
| Speed | Fast block-level copy | Slower; serialize → write → read → replay |
| Fidelity | Exact instance clone (users, extensions, sequences) | Logical only; some objects need care |
| Operational simplicity | Two API calls + poll | Multi-stage with intermediate storage |

For the POC's goal — "non-prod mirrors prod" — the snapshot approach is dramatically simpler and faster.

### Consequences (the important part)

Snapshots are **whole-instance and opaque**. This is not a limitation we can engineer around within the chosen approach — it is inherent:

- **No table/database-level filtering.** A backup is the entire instance; `restoreBackup` overwrites the entire target. There is no API to restore a subset. *If you need this, you must switch mechanisms — it cannot be bolted on.*
- **No in-flight data masking.** Prod data lands in non-prod byte-for-byte. Masking is only possible *after* the restore completes (a window where real PII sits in non-prod), or by switching to the logical-dump path. For regulated PII this window is often unacceptable.
- **Whole-instance overwrite.** Restore replaces everything including users — which is exactly why `reset_target_password` exists (to restore a stable non-prod credential afterward).
- **Tier/version coupling.** The target must be the same major PostgreSQL version and an equal-or-larger tier.

### When to revisit

If a future requirement needs **table-level granularity** or **masked PII in non-prod**, do **not** bolt it onto snapshots. Add a **second sync mode** built on `pg_dump`/`export` and let the user select per environment. That is a real architectural fork warranting its own ADR — not an incremental feature.

---

## Security notes

- Project IDs and instance names are validated against strict regex at startup to prevent log injection
- The prod≠target guard (applied per target) prevents the job from restoring prod onto itself even if misconfigured
- Service accounts follow least-privilege — they have no permissions outside Cloud SQL and Cloud Run
- `config.yaml` and `terraform.tfvars` are gitignored — real project IDs are never committed
- The job SA has no key file — it uses Workload Identity (Cloud Run's built-in identity)
