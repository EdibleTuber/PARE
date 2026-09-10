# Deploying ArcticBase for PARE

ArcticBase is consumed, not modified (spec §3), so nothing here edits its repo.
`docker-compose.override.yml` goes **next to** its `docker-compose.yml`; compose
picks that filename up automatically.

```bash
cp deploy/arcticbase/docker-compose.override.yml /path/to/ArcticBase/
cd /path/to/ArcticBase
sudo chown -R 1000:1000 ./data          # see below
sudo docker compose up -d --build
```

## Two things that stop it dead, both hit on this bench

**`pull access denied for arctic-base`.** The shipped `docker-compose.yml` has
`image: arctic-base:latest` with its `build:` stanza commented out, so compose
tries to pull an image that was never published — and `--build` has nothing to
build. The override supplies the build stanza.

**The container starts and immediately exits, nothing listening on 2929.**
Compose creates `./data` as `root:root`, and the image runs as uid 1000
(`arctic`). The Dockerfile does `chown -R arctic:arctic /data`, but that applies
to the *image layer* — a bind mount replaces that path, so the chown is
discarded and a uid-1000 process cannot write a root-owned directory. `chown -R
1000:1000 ./data` fixes it.

## Why the build stage matters, and how we found out the hard way

The Dockerfile is multi-stage: a Node stage runs `pnpm build` and the runtime
stage copies `/app/dist` into `/app/frontend/dist`. **That build stage is the only
thing that produces the web UI.**

Running the backend directly (`python -m arctic_base`) skips it. The API answers
perfectly — `/api/health`, `/api/workbenches/...` all 200 — while `/` and
`/wb/<slug>` return `{"detail":"Not Found"}`. Every test we had passed, because
they all talk to the API. It stayed invisible until a kiosk at the bench
redirected to a workbench page and displayed raw JSON.

So: **check both surfaces, every time.**

```bash
curl -s -o /dev/null -w "api %{http_code}\n" http://127.0.0.1:2929/api/health
curl -s -o /dev/null -w "ui  %{http_code}\n" http://127.0.0.1:2929/
```

The bench status page now enforces this itself — `probe_arcticbase` requires the
base URL to serve, not just `/api/health`, so it can never green-light a handoff
to a page that does not exist.

## The upload cap

The override sets `ARCTIC_BASE_MAX_UPLOAD_BYTES: 8388608` (§5.1); the default is
2 GB, which is exactly the failure that section describes. Verified in the
container: a 9 MiB `PUT /objects/{oid}/content` returns **413**.

It does **not** cover the JSON `POST /objects` path, which stores inline content
with no size check at all — which is why PARE publishes in two calls.
