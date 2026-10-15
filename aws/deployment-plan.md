# Kea data pipeline on AWS: deployment plan

**Status:** proposed, 16 Oct 2026; revised the same day for your own new AWS account (§1.1). Nothing
below is built yet.
**Scope:** the data pipeline (Sources → Lake → Warehouse → Serving, orchestrated by Airflow) on
managed AWS, with Snowflake kept. [Stack doc §2](../design/kea-data-engineering-stack.md) maps each local service to its AWS target; this
document says how to get there, what has to change in the code, what it costs and in what order.

**In one paragraph.** Deploy into **Sydney (ap-southeast-2)** from **your own new AWS account** (credits pay first), with
**Terraform**, as **sessions**: `make aws-up` creates everything, `make aws-run` runs the same
pipeline `make rebuild-all` runs locally, and `make aws-down` destroys everything that bills by the
hour. Between sessions only S3, ECR and parameters remain, for well under US$1 a month. MinIO becomes
S3, Spark standalone becomes **EMR Serverless**, the `DockerOperator` tasks become **ECS Fargate**
tasks, self-hosted Airflow becomes **MWAA** (Airflow 3.3.1), `kea-source-db` and `kea-db` become two
**RDS PostgreSQL** instances, and `.env` becomes **SSM Parameter Store**. A session costs about
US$2–4 in AWS charges plus the Snowflake credits it burns.

---

## 1. Decisions

The seven questions left open on 14 Oct, each answered with the default this plan assumes. Change
any of them and only the sections they name move.

| # | Question | Assumed | Why | Moves |
|---|---|---|---|---|
| 1 | Account | **Your own AWS account, opened Oct 2026**, CLI profile `kea-personal`. Root user with MFA, then locked away; one IAM admin user with MFA, signed in from the CLI with `aws login`. **Not IAM Identity Center** | the university `kea-energy` profile cannot create IAM roles and has a shared NZD 200 budget. Identity Center needs AWS Organizations, and joining Organizations upgrades a Free-plan account to Paid automatically | §1.1, §6 |
| 2 | Scope | **The data path**: S3, EMR Serverless, ECS Fargate, MWAA, two RDS, Snowflake. Metabase, Marquez, Grafana and streaming are later phases | it is the part the design calls managed AWS, and the part a data engineering reviewer will ask about | §4, §6 |
| 3 | Budget | **Spend the new-account credits; alert on gross cost.** A US$40 monthly cost budget with **credits excluded**, alerts at 50%, 80% and 100% (forecast) | credits make the net bill zero, so a budget that counts them never fires; this one says what a forgotten `aws-down` is costing within a day | §7 |
| 4 | Always-on or on demand | **On demand**: up, run, down | always-on MWAA small alone is ≈ US$360 a month | §5 |
| 5 | Snowflake after the trial (≈ 9 Nov) | **On-demand (pay-as-you-go) Standard**, X-Small, `AUTO_SUSPEND = 60` | the warehouse is only awake during a session | §7 |
| 6 | Timing | **After the UniMed application** (closes 25 Oct); Phase 0 can start any time | Phase 0 is an evening; Phases 1–3 are each a weekend | §6 |
| 7 | Infrastructure as code | **Terraform**, two root modules, `aws/bootstrap/` and `aws/session/`, beside this plan; state in S3 after Phase 0 | the team's app infrastructure (`HAKAS-kea-energy/infra/aws`) is already Terraform and you wrote most of it | §4 |

### 1.1 The account plan decides the architecture

A new AWS account is opened on one of two plans, shown under **Billing and Cost Management → Free
Tier**. Both get US$100 of credits at sign-up and up to US$100 more for completing activities.

| | **Paid plan** | **Free plan** |
|---|---|---|
| Services | all | "select" services only: AWS blocks what could burn through credits quickly. MWAA has no free tier, and EMR is reported as ineligible. Check both in the console before building |
| Charges | credits pay first, then pay-as-you-go | never; the account **closes** after 6 months or when the credits run out, and is deleted 90 days later unless upgraded |
| This plan | **as written** | changes below |

**On the Paid plan** nothing in §2–§5 moves. US$100 covers roughly 35 sessions (§7).

**On the Free plan**, three changes, or upgrade (one click, one way, and the credits carry over):
- **Airflow:** MWAA becomes the same Airflow 3.3 image, self-hosted on one EC2 `t4g.medium` in
  the session VPC with its metadata DB on the source RDS. §3.6's packaging is skipped, and §3.1's
  factory still switches to ECS and EMR or Fargate.
- **Spark:** if EMR Serverless is blocked, `interval_reads` runs as `spark-submit --master
  local[*]` in a Fargate task (4 vCPU, 16 GB). That is enough for 200 households. The EMR switch in
  §3.1 waits for an upgrade.
- **Nothing survives the account:** the lake, images and Terraform state are deleted with it, so
  `bootstrap/` keeps a copy of its state locally, and the plan relies on `init-sources` to rebuild
  everything.

**Region.** Sydney, not Auckland. Auckland (ap-southeast-6) is opt-in and has EMR Serverless and
Fargate, but classic MWAA could not be confirmed there (only MWAA Serverless is listed). The Snowflake
account is also in ap-southeast-2, so Sydney keeps every Snowflake round trip in-region. The data is
LCL (London) and synthetic, so there is no NZ residency requirement. Revisit when classic MWAA reaches Auckland.

**Why not MWAA Serverless.** It is billed per task-minute, which suits this workload, but it runs
**YAML** workflow definitions, not Python DAGs. The six DAGs here use asset scheduling, a launcher
factory and Python callables that import `kea_pipeline`. Rewriting them as YAML gives up the
property stack doc §2 promises: "DAGs and dbt models cannot tell which they run against." Classic
MWAA run as a session costs a few dollars and keeps the DAGs as they are.

---

## 2. Target architecture

```
                       ┌──────────────────────── VPC 10.40.0.0/16, ap-southeast-2 ──────────────────────┐
 laptop ── make aws-* ─┤                                                                                │
   (Terraform, AWS CLI)│  public subnets a/b ── NAT gateway ── internet ── Snowflake (ap-southeast-2)   │
                       │                                                                                │
                       │  private subnets a/b                                                           │
                       │   MWAA (Airflow 3.3.1) ──launches──► ECS Fargate tasks (dbt image, soda image)  │
                       │        │                 └─launches─► EMR Serverless (Spark 3.5, interval_reads)│
                       │        │                                                                       │
                       │   RDS kea-source (PG 17) ◄── extract roles      RDS kea-db (PG 16) ◄── publish   │
                       │                                                                                │
                       │   S3 gateway endpoint ──► s3://<prefix>-kea-lake  (bronze/, parsed/, export/)  │
                       └────────────────────────────────────────────────────────────────────────────────┘
   kept between sessions:  S3 lake + MWAA bucket, ECR repos, SSM parameters, Terraform state
```

| Local (`docker-compose.yml`) | AWS, this plan | Notes |
|---|---|---|
| MinIO, bucket `kea-lake` | S3 bucket `<prefix>-kea-lake`, versioning off, SSE-S3, lifecycle on `_spark/` and `tmp/` | bucket names are global; `KEA_LAKE_BUCKET` already parameterises it |
| Spark master + worker | EMR Serverless application, release emr-7.x (Spark 3.5), **custom image** from ECR | `max_capacity` 8 vCPU / 32 GB; auto-stop after 5 idle minutes |
| `DockerOperator` (dbt, extract_small, publish, export) | `EcsRunTaskOperator`, Fargate, ARM64 where the images allow | one task definition per image, command per task |
| Airflow 3.3.0 + its Postgres | MWAA `mw1.small`, Airflow **3.3.1**, private web server | DAGs, `lib/` and `scripts/` synced to the MWAA S3 bucket |
| `kea-source-db` (PG 17.10) | RDS PostgreSQL 17, `db.t4g.micro`, 20 GB gp3, Single-AZ | stands in for the four source systems; seeded by `init-sources` |
| `kea-db` (PG 16) | RDS PostgreSQL 16.15, `db.t4g.micro`, 20 GB gp3, Single-AZ | same version as the team app's RDS; migrations unchanged |
| `.env`, Airflow connections, Vault | SSM Parameter Store (SecureString, standard tier) + MWAA's `SystemsManagerParameterStoreBackend` | free at this size; Secrets Manager would be ≈ US$0.40 per secret per month |
| Snowflake key file bind-mounted from the host | parameter `/kea/snowflake/private_key`, injected into ECS tasks as a secret | see §3.4 |
| Marquez, Prometheus, Grafana, alert-sink | CloudWatch Logs (MWAA, ECS, EMR) in Phase 1; Marquez on ECS in Phase 4 | |
| Metabase | Phase 4: the team app's EC2 pattern (Metabase on a `t3.medium`) or keep it local over a tunnel | |
| Kafka + Flink | out of scope (MSK alone outspends the whole budget) | |

**IAM, least privilege.** Four roles, each named for what assumes it:
`kea-mwaa-execution` (read its bucket, start ECS tasks and EMR jobs, pass the two roles below, read
`/kea/airflow/*`), `kea-ecs-task` (lake read/write, `/kea/*` parameters its task names),
`kea-emr-job` (lake read/write, read the metering source parameter), and `kea-ecs-execution` (pull
ECR, write logs). The source-system isolation that `assert_isolation.sh` proves is **database
roles**, not IAM, so it carries over unchanged: each extract task still receives exactly one
system's credential.

---

## 3. What changes in the code

The design already expects most of this (stack doc §2, "Where the substitution is not free").
Everything below is switched by **one variable, `KEA_RUNTIME=local|aws`**, defaulting to `local`, so
`make ci`, `make rebuild-all` and the local stack keep working byte for byte.

### 3.1 The launcher factory (the largest change)

`pipeline/airflow/dags/_kea_common.py::_container` and
`pipeline/airflow/dags/kea_extract_metering.py::_spark_task` are today the only two places a task
becomes a container. Make them the switch the stack doc asks for:

- `_container(...)` → `DockerOperator` locally, `EcsRunTaskOperator` on AWS (same image name
  resolved to its ECR URI, same command, same environment, `awsvpc` in the private subnets,
  `wait_for_completion=True`, logs to CloudWatch).
- `_spark_task(...)` → `DockerOperator` locally, `EmrServerlessStartJobOperator` on AWS
  (`entryPoint` the job in S3, `--py-files` a zip of `lib/` and `contracts/` laid out as siblings,
  because the schema loader resolves contracts relative to its own file).
- Move `_spark_task` into `_kea_common.py` beside `_container`. Its docstring already says
  "divergence there is how two ways of launching a container become two sets of bugs".

### 3.2 No more bind mounts: bake the code into the images

Every task mounts `HOST_PROJECT_DIR` read-only. Fargate has no host, so the **dbt image** gains a
`COPY` of the repository subset it runs (`pipeline/`, `migrations/`, `design/serving_read_contract.json`)
and is tagged `git-<sha>`, the same release id `make deploy` uses. `make aws-images` builds and
pushes it, the soda image and the Spark image. Locally the mount still wins, so nothing changes there.

### 3.3 Lake access: the default AWS credential chain instead of MinIO keys

- `pipeline/spark_jobs/spark_session.py`: when `MINIO_ENDPOINT` is unset, drop the endpoint,
  path-style access and `SimpleAWSCredentialsProvider`. EMR Serverless supplies credentials from the
  job role.
- `pipeline/lib/kea_pipeline/lake.py` (the boto client): same rule. No endpoint and no keys means
  boto's default chain, which is the ECS task role.
- Asset URIs (`s3://kea-lake/...`) are names, not paths. Keep them so lineage stays comparable
  across runtimes.

### 3.4 Snowflake

- **Key.** `profiles.yml` reads `SNOWFLAKE_PRIVATE_KEY_PATH`. On ECS the key arrives as a secret
  environment variable, so the entrypoint writes it to `/tmp/snowflake.p8` (mode 0600) and sets
  the path. The profile and the connector code stay as they are.
- **Loading and export stay push and pull through the connector** for Phase 1–3. They work from AWS
  unchanged, and the loader's comment on why there is no external stage (MinIO not reachable) stops
  being true. **Phase 4** can then do what design §7 originally said: a storage integration on the
  lake bucket, `COPY INTO` from an external stage, and `COPY INTO s3://.../export/`. That is a
  contained improvement, not a prerequisite.
- **Network policy.** Allow the NAT gateway's Elastic IP (and your home IP) on the `KEA` service
  user.

### 3.5 Scripts that `docker compose exec`

`set_extract_roles.sh`, `set_lake_audit_role.sh`, `assert_isolation.sh`, `seed_fixtures.sh` and
others reach Postgres with `docker compose exec … psql`. Add a single **ops task definition** (dbt
image, which has `psql`) and a `pipeline/scripts/ops/aws_exec.sh` wrapper that runs one command in it
and streams its CloudWatch log. Under `KEA_RUNTIME=aws` the scripts call that wrapper instead of
`docker compose exec`. Same scripts, same order.

### 3.6 Airflow on MWAA

- **Version.** MWAA offers 3.3.1 on Python 3.12; local is 3.3.0 on Python 3.11. Move local to
  `apache/airflow:3.3.1-python3.12` first, so CI's DAG-import test runs the version MWAA will.
- **Packaging.** MWAA reads DAGs from `s3://<mwaa-bucket>/dags/`. `make aws-dags` syncs
  `pipeline/airflow/dags/`, plus `lib/kea_pipeline` and `scripts/lake` under `dags/_kea/`. The two
  `sys.path.insert` lines in `_kea_common.py` resolve relative to the DAG file instead of
  `/opt/airflow`. `requirements.txt` adds the Amazon provider pinned to MWAA's 3.3.1 constraints
  and drops the Docker provider on AWS.
- **Connections.** `AIRFLOW_CONN_*` move to `/kea/airflow/connections/*`, read by the Parameter
  Store secrets backend. DAG code doesn't change.
- **`kea_daily_pipeline`'s `dashboards` group** refreshes rollups in `kea-db`. That's an ECS task too.
  Building the Metabase cards stays out until Phase 4.

---

## 4. Infrastructure: `aws/` (Terraform)

```
aws/
  deployment-plan.md   this document
  bootstrap/      once per account: state bucket, budget + alerts, ECR repos, the lake and MWAA buckets,
                  GitHub OIDC role (optional). Never destroyed by aws-down.
  session/        everything that bills by the hour: VPC, NAT, endpoints, RDS ×2, EMR Serverless app,
                  ECS cluster + task definitions, MWAA environment, IAM roles, security groups.
  README.md       the runbook in §5
```

- **Two states, on purpose.** `bootstrap/` holds what must survive a teardown (the lake, images,
  parameters); `session/` is created and destroyed whole. A `terraform destroy` on `session/` can
  never take the lake with it. That is the same split the team app uses for its Elastic IP
  (`address/`).
- **VPC.** Two AZs (MWAA requires two private subnets), one NAT gateway (not one per AZ), an S3
  **gateway** endpoint (free, keeps lake traffic off the NAT), no interface endpoints (each costs more
  per hour than the NAT saves here).
- **RDS between sessions.** Destroy without a final snapshot, and re-seed with `init-sources` each
  session (9.5 minutes measured at 200 households). It's cheaper than snapshot storage, and it
  re-proves the Sources step every time. Set `KEEP_SNAPSHOT=1` to keep one instead.
- **Tags.** `project=kea`, `session=<date>` on everything; the budget filters on `project`.

---

## 5. A session (the runbook)

```
make aws-images            # build + push dbt, soda, spark images as git-<sha>   (only after code changes)
make aws-up                # terraform apply session/ (~25–35 min, MWAA dominates), then sync DAGs
make aws-run               # init-sources → ingest → warehouse → serving → rollups, all through MWAA/ECS/EMR
make aws-status            # MWAA run states, the last gate results, the session's cost so far
make aws-down              # terraform destroy session/ (~20 min); the lake and images remain
```

**The wrong account is refused.** Every `aws-*` target runs with `AWS_PROFILE=kea-personal` and first
compares `aws sts get-caller-identity` with `KEA_AWS_ACCOUNT_ID` in `.env`. On any other account,
including the university one behind the `kea-energy` profile, it stops before Terraform runs.

`aws-run` runs the same chain `rebuild-all` runs, with the same gates: `assert_isolation`, `assert_bronze`,
the Soda warehouse gate, `assert_serving` and the Soda serving report. A run counts as deployed only
when those pass on AWS. Expect about 20 minutes, as measured locally. Most of it is Sources and
Snowflake work, not compute.

**Two guards against a forgotten `aws-down`:**
1. `aws-up` records the session start in `/kea/session/started_at` and prints the hourly burn.
2. An EventBridge schedule in `bootstrap/` runs a small Lambda every hour. If a `project=kea` MWAA
   environment is older than **6 hours**, it emails you through SNS. It does not delete anything,
   because deleting is your call (same rule as the team app's "no automatic termination timer").

---

## 6. Phases

Each phase ends in something that can be shown, and none needs the next one.

| Phase | Work | Done when |
|---|---|---|
| **0 Account** (an evening) | read the account plan (§1.1); root MFA; IAM admin user with MFA; `aws login --profile kea-personal`; check the quotas a new account starts low on (Fargate On-Demand vCPU, EMR Serverless concurrent vCPU, RDS instances) and request increases; `bootstrap/` (state bucket, budget excluding credits, ECR repos, lake + MWAA buckets) | `aws sts get-caller-identity --profile kea-personal` returns the new account's ID; the budget alert email arrives on a test threshold |
| **1 Lake on S3** (weekend 1) | §3.3, §3.2, the ops runner (§3.5); `session/` without MWAA: VPC, NAT, two RDS, ECS cluster, EMR app | `make aws-run STEP=lake` runs from the laptop: ECS and EMR tasks, Parquet in S3, `assert_bronze` green |
| **2 Warehouse + Serving** (weekend 2) | §3.4; `build-warehouse` and `publish-serving` as ECS tasks; Snowflake network policy | Soda warehouse gate, `assert_serving` and the Soda serving report green against RDS `kea-db` |
| **3 Orchestration** (weekend 3) | §3.1, §3.6; MWAA in `session/`; local Airflow → 3.3.1 | the four `kea_extract_*` DAGs and `kea_daily_pipeline` complete on MWAA; `make ci` still green; a GitHub Actions job, run on demand, can deploy DAGs by OIDC (no stored keys) |
| **4 Optional** | Snowflake storage integration + external stages (§3.4); Metabase on EC2 over RDS; Marquez on ECS; CloudWatch dashboard and alarms in place of Grafana | each one on its own |

Phases 1–2 still run without MWAA, so a working "Spark on EMR Serverless → S3 → Snowflake → RDS"
path exists after two weekends even if Phase 3 slips.

---

## 7. Cost

Rates are on-demand, ap-southeast-2, USD, **to be re-checked in the AWS Pricing Calculator during
Phase 0**. The MWAA rate is the published small-environment price, and the rest are approximate.

| While a session is up | Per hour |
|---|---|
| MWAA `mw1.small` (Phase 3 on) | ≈ 0.49 |
| NAT gateway (+ data processed, small here) | ≈ 0.06 |
| RDS `db.t4g.micro` ×2 | ≈ 0.05 |
| ECS Fargate tasks (seconds to minutes each) | < 0.05 |
| EMR Serverless (`interval_reads`, 200 households: minutes of a few vCPU) | < 0.10 |
| **Total** | **≈ 0.65–0.75** |

| Per month, idle | |
|---|---|
| S3 lake + MWAA bucket (a few GB) | < 0.20 |
| ECR (three images, ≈ 3 GB) | ≈ 0.30 |
| SSM Parameter Store standard, Budgets, Lambda guard | 0 |
| **Total** | **< US$1** |

**A session** (≈ 3 hours: 35 min up, 20 min run, time to show it, 20 min down) is **≈ US$2–3**, paid from credits first: US$100 is about 35 sessions. The credits' expiry date is under Billing → Credits.
Four a month is ≈ US$10–12, inside the US$40 alert and the US$100 sign-up credit. **Snowflake** is billed separately: an
X-Small warehouse uses 1 credit per hour while awake, and with 60-second auto-suspend a run uses
roughly 0.3–0.5 credits. Check the on-demand credit price for Standard edition in AWS Sydney before
the trial ends.

**What would blow the budget:** forgetting `aws-down` (≈ US$17 a day, which the 6-hour guard and the
budget alert catch), turning on NAT-per-AZ or interface endpoints, MSK, or a Snowflake warehouse
without auto-suspend.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| MWAA environment creation fails or is slow (VPC/IAM mistakes surface 20+ min in) | Phase 1–2 never need MWAA; validate the VPC with `aws mwaa` pre-checks; keep the network module identical to AWS's MWAA reference VPC |
| Python 3.12 / provider pins break a DAG import | move local Airflow to 3.3.1-python3.12 first; `make ci ONLY=dags` must pass before any `aws-dags` |
| EMR custom image drifts from the local Spark image | build both from `pipeline/spark_jobs/Dockerfile` with a different base, one `constraints.txt` |
| A teardown leaves something billing (ENIs held by MWAA delay subnet deletion) | `aws-down` re-runs destroy until clean, then lists any `project=kea` resource still present |
| A new account's low default quotas stop ECS, EMR or RDS creation | check and request increases in Phase 0. Increases can take days |
| Free plan: the account closes at 6 months or when the credits run out, taking the lake with it | Paid plan, or treat the account as disposable (§1.1) |
| Snowflake trial ends mid-work (≈ 9 Nov) | convert to on-demand before Phase 2, or note that Phase 1 needs no Snowflake |
| A credential ends up in Terraform state | state bucket encrypted and private; database passwords generated by RDS-managed secrets or `random_password` written straight to SSM, never to outputs |

---

## 9. Before starting

1. Read the account plan (§1.1) and confirm the other defaults in §1.
2. Phase 0 checks: classic MWAA in ap-southeast-2 on 3.3.1 (expected yes), current Sydney rates in
   the Pricing Calculator, Snowflake on-demand price, EMR Serverless release with Spark 3.5 and
   Python 3.11/3.12.
3. Create the branch `aws/phase-0` and add `aws/bootstrap/`.
