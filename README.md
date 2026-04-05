# Lap Vision F1 Service

Internal Python service for Formula 1 ingestion, normalization and disk caching on top of `Fast-F1`.

## Goals

- keep `Fast-F1` isolated from the Go product API
- load and cache historical F1 data on demand
- expose normalized internal endpoints that `lap_vision` can call
- avoid single-process bottlenecks by offloading heavy loads to a process pool

## Current scope

- service overview and health endpoints
- season schedule endpoint
- historical session bundle endpoint
- disk cache management for:
  - native `Fast-F1` cache
  - normalized JSON payload cache

## Layout

- `app/main.py`: FastAPI entrypoint
- `app/config.py`: environment-driven settings
- `app/cache.py`: normalized cache layer helpers
- `app/services/historical.py`: heavy `Fast-F1` workers and async orchestration
- `app/routes/f1.py`: internal HTTP routes

## Environment

Optional env vars:

- `LAP_VISION_F1_HOST=0.0.0.0`
- `LAP_VISION_F1_PORT=8010`
- `LAP_VISION_F1_WORKERS=2`
- `LAP_VISION_F1_FASTF1_CACHE_DIR=./var/fastf1-cache`
- `LAP_VISION_F1_DATA_CACHE_DIR=./var/data-cache`
- `LAP_VISION_F1_INTERNAL_TOKEN=dev-token`

## Run

```bash
cd /Users/evgenylugin/py/lap-vision-f1
make run
```

Useful commands:

```bash
make help
make lint
make health
make overview
make schedule YEAR=2025
make session YEAR=2025 EVENT="Australian Grand Prix" SESSION=R
```

Smoke checks:

```bash
make test
make check
```

Config lives in `.env.local`. If the file is missing, `make run` creates it from `.env.example`.

## Example requests

```bash
curl -H 'X-Internal-Token: dev-token' http://localhost:8010/healthz
curl -H 'X-Internal-Token: dev-token' http://localhost:8010/v1/overview
curl -H 'X-Internal-Token: dev-token' http://localhost:8010/v1/seasons/2025/schedule
curl -X POST -H 'Content-Type: application/json' -H 'X-Internal-Token: dev-token' \
  http://localhost:8010/v1/sessions/load \
  -d '{"year":2025,"event":"Australia","session":"R"}'
```

## Next integration step

`lap_vision` should call `lap-vision-f1` for historical ingestion jobs, then persist normalized payloads into the F1 tables created in the Go backend migrations.

## Deployment

`lap-vision-f1` is deployed through GitHub Actions with a manual `workflow_dispatch` blue/green workflow.

- runtime is internal-only on the host
- no public port is published
- an internal nginx router container `lapvision-f1-router` exposes `http://lapvision-f1-router:8010` on the shared Docker network
- `lap_vision` should use that internal URL in production

Required GitHub secrets:

- `DEPLOY_HOST`
- `DEPLOY_PORT`
- `DEPLOY_USER`
- `DEPLOY_PATH`
- `DEPLOY_SSH_KEY`
- `F1_INTERNAL_TOKEN`

Optional GitHub variables:

- `F1_WORKERS` default `2`
