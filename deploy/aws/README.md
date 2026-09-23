# Deploying KairoX on AWS — ECS Fargate behind an ALB

> **These are TEMPLATES, not a working deployment.** Every `TODO:` below is a
> value only you have. Nothing here has been applied or tested against an AWS
> account — it could not be, from a laptop with no credentials. Treat it as the
> shape of the deployment and the reasoning behind each choice, then fill in the
> blanks and apply it yourself.

## Why this shape

You already build a Docker image and deploy it with `docker compose` on one
server. The smallest honest step from there to something that scales is ECS
Fargate: the *same image* runs all three services, distinguished only by the
command, which `entrypoint.sh` already supports (`api` / `worker` / `beat`).

```
Route 53 ──► ALB (TLS)
               ├── ECS service "api"     N tasks, autoscaled
               ├── ECS service "worker"  Celery, scales independently
               └── ECS service "beat"    PINNED AT EXACTLY 1
                        │
                 RDS Proxy ──► RDS PostgreSQL
                 ElastiCache Redis   (broker + cache + login throttle)
                 S3 (uploads)   ECR (image)   Secrets Manager
```

## The three things that will bite you

**1. `beat` must be exactly one task.** Each replica is its own scheduler, so two
beat tasks fire every periodic job twice. `docker-compose.prod.yml` already
carries this warning. Set `desiredCount: 1`, and no autoscaling policy.

**2. Migrations must run BEFORE the new tasks start, not inside them.** With
`RUN_MIGRATIONS=true` every api task migrates on boot; N tasks race on the same
revision. Run the one-off task first (below), then deploy with
`RUN_MIGRATIONS=false`.

**3. RDS Proxy is not optional once you scale.** Each task holds
`DB_POOL_SIZE + DB_MAX_OVERFLOW` connections. Six tasks at 5+5 is 60 client
connections against a Postgres whose `max_connections` may be 100 — and that is
before the workers. The Proxy multiplexes them onto a much smaller server-side
set, which is what lets you add tasks without running out.

## Sizing the pool

```
concurrent requests served = tasks × (DB_POOL_SIZE + DB_MAX_OVERFLOW)

  6 tasks × (5 + 5) = 60 burst, 30 sustained
```

A request holds its connection for its whole lifetime, so this — not CPU — is
the ceiling. Raise `DB_POOL_SIZE` before raising task count, and watch RDS
Proxy's `DatabaseConnectionsCurrentlySessionPinned`: a high number there means
something is pinning sessions and defeating the multiplexing.

## Environment

Set these on the task definition. Secrets belong in Secrets Manager, referenced
by ARN — never inline in the task definition, which is readable by anyone with
`ecs:DescribeTaskDefinition`.

| Variable | Value | Notes |
|---|---|---|
| `ENVIRONMENT` | `production` | Drops the extra localhost CORS origins |
| `DEBUG` | `false` | `true` would run `create_all` at boot — schema drift |
| `DOCS_ENABLED` | `true` | `/docs` + `/redoc` stay ON by design (2026-09-23) |
| `SECRET_KEY` | *secret* | Boot FAILS on a placeholder — in EVERY environment |
| `DATABASE_URL` | *secret* | via RDS Proxy endpoint, `postgresql+psycopg2://` |
| `ASYNC_DATABASE_URL` | *secret* | same host, `postgresql+asyncpg://` |
| `CORS_ORIGINS` | `https://frontend-rust-pi-23.vercel.app,https://stagingpte.vercel.app,http://localhost:3005,http://localhost:3012` | Unset falls back to exactly this list (`config.py:_DEFAULT_CORS_ORIGINS`) |
| `TRUSTED_HOSTS` | TODO: your domain(s) | Blank answers to any Host header |
| `CELERY_BROKER_URL` | *secret* | ElastiCache, `rediss://` for TLS |
| `CACHE_ENABLED` | `true` | Dashboard cache; busted on write, not on a timer |
| `STORAGE_BACKEND` | `s3` | **Already implemented** — see below |
| `S3_BUCKET` / `S3_REGION` | TODO | |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `5` | See sizing above |
| `WEB_CONCURRENCY` | `2` | Gunicorn workers *per task*. Multiply into the pool maths. |
| `RUN_MIGRATIONS` | `false` | See #2 above |
| `RUN_SEED` | `false` | Never seed production |

### Uploads are already S3-capable

`app/core/storage.py` implements `S3StorageBackend` with lazy boto3 and real
presigned URLs. Container-local disk is a *configuration* state, not a missing
feature: set `STORAGE_BACKEND=s3` plus bucket and region, give the task role
`s3:GetObject`/`PutObject`/`DeleteObject` on the bucket, and add `boto3` to
requirements (it is currently commented out as optional).

## Health checks

The ALB target group should check `/health`. Point the **container** health
check at `/ready` if you want a task to be replaced when it cannot reach the
database — `/health` answers while degraded, which is what you want for the
load balancer but not for a liveness decision.

```
HealthCheckPath: /health
HealthCheckIntervalSeconds: 15
HealthyThresholdCount: 2
UnhealthyThresholdCount: 3
Matcher: 200
```

## Autoscaling

Target-tracking on `ALBRequestCountPerTarget` rather than CPU. This app is
IO-bound on the database; CPU stays low while the pool saturates, so a CPU
policy would never scale at the moment you actually need it.

```
TargetValue: TODO  — measure first. Load test at 25/50/100/200 VUs,
                     find the request rate per task at which p95 degrades,
                     and set the target at ~70% of it.
MinCapacity: 2      — two tasks so a deploy or an AZ loss is not an outage
MaxCapacity: TODO   — bounded by RDS Proxy's client connection limit
ScaleInCooldown: 300
ScaleOutCooldown: 60   — out fast, in slow
```

## Deploy sequence

```bash
# 1. build and push
docker build -t kairox:$GIT_SHA .
docker tag kairox:$GIT_SHA $ECR/kairox:$GIT_SHA     # TODO: $ECR
docker push $ECR/kairox:$GIT_SHA

# 2. MIGRATE FIRST, as a one-off task, and wait for it to exit 0
aws ecs run-task \
  --cluster TODO \
  --task-definition kairox-migrate \
  --launch-type FARGATE \
  --overrides '{"containerOverrides":[{"name":"api","command":["migrate"]}]}' \
  --network-configuration 'TODO: awsvpcConfiguration'

# 3. only then roll the services
aws ecs update-service --cluster TODO --service kairox-api \
  --task-definition kairox-api:NEW --force-new-deployment
```

`migrate` is an existing `entrypoint.sh` command: it waits for Postgres, runs
`alembic upgrade head`, and exits.

## What is NOT here, and why

- **Terraform / CDK.** You did not say which you use, and a half-guessed module
  is worse than none.
- **A load test.** It needs a running staging environment. The plan is 25 / 50 /
  100 / 200 VUs against `/production/log` and the dashboards, recording p50/p95/
  p99 and error rate, before and after.
- **Index tuning.** Needs `EXPLAIN ANALYZE` against Postgres with realistic
  volumes. The one index that was obviously needed — attendance
  `(employee_id, work_date)` — already exists as a `UniqueConstraint`.
