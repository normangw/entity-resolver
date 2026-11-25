# Scaling

The service runs as one api process on one instance. This page says what would
break if we ran two copies behind one address, how we would fix each part, and how
the result could run on Kubernetes. It is a description only; nothing here is
built.

## What one process holds in memory

| State | Where | What goes wrong with two copies |
|---|---|---|
| Rate-limit buckets | `protection.py` | Each copy counts on its own, so a client gets twice the limit (N times with N copies). |
| Wikidata answer cache | `wikidata.py` (TTL 1 h) | Each copy asks Wikidata for the same names again: more calls, more throttling, and two copies can briefly give different answers for a name whose Wikidata entry just changed. |
| Circuit breaker | `wikidata.py` | One copy can be open while the other keeps calling a Wikidata that is down or throttling us. |
| Wikidata concurrency limit (semaphore, 4) | `main.py` | Becomes 4 per copy, so 8 lookups at once with two copies. Wikidata sees more pressure than we planned for. |
| GLEIF daily budget | `gleif.py` | Each copy spends the full budget, so the courtesy cap doubles. |
| Upload jobs: the job table and running tasks | `uploads.py` | A job lives in the memory of the copy that received the upload. If the next request (`/preview`, `/status`, `/download`) goes to the other copy, it gets `404`. |
| Upload files | the `uploads` volume | A Docker volume on one host, or a ReadWriteOnce disk in Kubernetes: only one copy can mount it read-write. |
| Metrics counters | `observability.py` | Fine if Prometheus scrapes each copy separately and sums; wrong if it scrapes the load-balanced address (it sees one copy at random). |

Stateless parts scale without change: the alias index (read-only, each copy loads
its own), the resolver, the settings, and `/health` and `/ready`.

## Fixes

1. **Shared fast store (Redis) for the small counters**: rate-limit buckets (a
   fixed-window or token-bucket script), the circuit breaker (failure count and
   open-until time), the GLEIF budget (a counter that expires at midnight UTC), the
   Wikidata cache (keys with a TTL), and a global Wikidata limit (a counter with a
   short expiry, or a Redis semaphore). Each is a few keys; the code already keeps
   them behind small classes, so the change is local.
2. **Uploads: object storage, a queue and a worker.** Put input and result files in
   an S3-compatible bucket (Jetstream offers object storage) instead of a local
   volume. Keep job state in Redis or a small database. `POST /upload/{id}/resolve`
   puts a message on a queue; a separate worker process resolves the file and writes
   the result. Any api copy can then answer status and download. The owner check
   stays the same (the hash of the cookie token is stored with the job).
3. **Sticky sessions** are a cheaper stopgap for uploads only: the load balancer
   sends a browser to the same copy every time. They fail when that copy restarts,
   so they do not replace step 2.
4. **Metrics**: scrape each copy (Prometheus service discovery), and sum in the
   queries.

With these, the api is stateless and copies can be added or removed freely. The
Wikidata limit then becomes the real ceiling: more copies do not make Wikidata
answer faster, so the shared limit in step 1 matters most.

## How Kubernetes would host it

- **Deployment** `entity-resolver-api` with 2 or more replicas, the GHCR image by
  version, settings from a **ConfigMap** and the API key from a **Secret**
  (`envFrom`). `ENVIRONMENT=production` stops a pod that is missing the key.
- **Probes**: liveness on `GET /health` (answers as soon as the process is up),
  readiness on `GET /ready` (200 only once the index is loaded), so no traffic goes
  to a pod that is still loading. A startup probe gives the index load up to a
  minute.
- **Service** (ClusterIP) in front of the pods, and an **Ingress** (for example
  ingress-nginx) with **cert-manager** for Let's Encrypt certificates. This replaces
  Caddy; the body limits, timeouts and headers move to Ingress annotations.
- **Index**: built into a read-only volume (a PVC with ReadOnlyMany, or downloaded
  from object storage by an init container at start). Swapping the index is a new
  volume or object and a rolling restart.
- **Uploads**: object storage and a separate **worker Deployment** reading the
  queue, as above. No ReadWriteOnce volume is shared.
- **Redis**: a managed instance or a StatefulSet, for the shared state above.
- **Backups**: a **CronJob** that runs `backup_index.py` and copies the archive to
  object storage in another region.
- **Scaling**: a HorizontalPodAutoscaler on CPU (local scoring is CPU-bound) with a
  low maximum, because Wikidata, not our CPU, limits the fallback path.
- **Rollouts**: a rolling update with `maxUnavailable: 0`, so readiness keeps old
  pods serving until new ones are ready; `kubectl rollout undo` replaces
  `deploy.sh --previous`.
- **Observability**: Prometheus discovers pods through annotations or a
  ServiceMonitor; Grafana as now.
