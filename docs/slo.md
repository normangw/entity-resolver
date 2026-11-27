# Entity Resolver observability and SLO

## Service-level objective

Proposed initial objective: over a rolling 30-day window, at least **95% of observed POST /resolve requests complete within 1 second**. This is a latency objective, not an accuracy or availability guarantee. All responses (including errors) are counted, and HTTP errors are monitored separately. A fast error can satisfy this latency objective; the error panel must be reviewed alongside it. This initial threshold is a proposed target, not a measured baseline. Revisit it after the Jetstream drills, especially for Wikidata fallback requests.

SLI: the fraction of observed requests in the 1-second histogram bucket over all observed requests. Do not interpret absent samples or a zero denominator as success. Prometheus can only observe requests recorded by the API process, and a server restart between scrapes can lose some observations.

```promql
sum(increase(http_request_duration_seconds_bucket{job="entity-resolver",method="POST",path="/resolve",le=~"1(\\.0)?"}[30d])) / sum(increase(http_request_duration_seconds_count{job="entity-resolver",method="POST",path="/resolve"}[30d]))
```

The dashboard panel **SLO: POST /resolve within 1 s (30 days)** uses the same expression, formatted as a percentage with a 95% threshold. The retention configuration must preserve the entire 30-day window for a full evaluation; the configuration retains up to 35 days, subject to a 1 GB size cap; operators should check disk capacity and effective retention. A fresh deployment has a partial window until enough time has elapsed.

Supporting queries:

```promql
# POST /resolve 5xx rate (server-side errors)
sum(rate(http_errors_total{method="POST",path="/resolve",status=~"5.."}[5m]))
# Cache hit ratio (undefined without lookups)
sum(rate(wikidata_cache_requests_total{result="hit"}[5m])) / sum(rate(wikidata_cache_requests_total[5m]))
```

## Start monitoring on Jetstream

Run only on a team-approved test deployment; do not replace an existing live stack without coordinating. The `observability` profile is opt-in, preserving the default API deployment. The API must include the companion Bearer authentication change and its configured API key must match the token file. Never commit credentials or put them in the Docker build context.

Create the secret files in a private directory outside the repository. From the repository root on Jetstream, use Bash (the values are entered without echo):

```bash
install -d -m 700 ../entity-resolver-observability-secrets
read -rsp 'Existing API_KEY: ' token; echo
printf '%s' "$token" > ../entity-resolver-observability-secrets/metrics-token
unset token
read -rsp 'Choose a strong Grafana admin password: ' password; echo
printf '%s' "$password" > ../entity-resolver-observability-secrets/grafana-admin-password
unset password
chmod 644 ../entity-resolver-observability-secrets/metrics-token ../entity-resolver-observability-secrets/grafana-admin-password
```

The private parent directory prevents other ordinary host users from accessing the files; readable file modes permit non-root container users to read the mounted secrets. Use the existing deployed API key, and rotate credentials if exposed. The Grafana password initializes a new data volume; an existing Grafana volume may retain its previous password.

```bash
docker compose --profile observability config --quiet
docker compose --profile observability up -d --build
docker compose --profile observability ps
docker compose exec prometheus promtool check config /etc/prometheus/prometheus.yml
docker compose exec prometheus wget -qO- 'http://127.0.0.1:9090/api/v1/targets'
```

Check the API target is `up` with no scrape error. Send authorized synthetic requests to `/resolve`, then inspect request counts, latency and the SLO panel. Prometheus has no published host port. Grafana binds only to loopback; use an SSH tunnel from your laptop (replace the login and host with your authorized Jetstream details):

```bash
ssh -N -L 3000:127.0.0.1:3000 USER@JETSTREAM_HOST
```

Open http://localhost:3000 and sign in as `admin` with the configured password. Open the Entity Resolver Observability dashboard. Do not open port 3000 to the public internet. Never use `docker compose down -v` for this check: it would remove named volumes, including uploads and the alias index.
