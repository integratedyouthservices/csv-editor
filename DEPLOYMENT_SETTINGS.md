# Live environment settings

Settings changes for the GCP deployment (Cloud Run + external HTTPS ALB + IAP).

**Problem they fix:** the websocket connection is torn down on a fixed cadence and
reconnects. Streamlit disables *every* widget while the connection state is not
`CONNECTED`, so the toolbar — Export CSV, Import CSV, Publish Changes — greys out for
about a second each time.

Replace `BACKEND_NAME`, `SERVICE_NAME`, and `REGION` with the real values.

---

## 1. Load balancer: backend service timeout

**Default 30s → 3600s.** For WebSocket traffic, GCLB treats the backend service timeout
as the *maximum lifetime of the connection*, idle or not — not an idle timeout. The 30s
default forces a reconnect every 30 seconds. **This is the main fix.**

```bash
gcloud compute backend-services update BACKEND_NAME \
  --global --timeout=3600
```

## 2. Load balancer: session affinity

**Default `NONE` → `GENERATED_COOKIE`.** Without it, a reconnect can land on a different
Cloud Run instance, which has no copy of the session. Unpublished edits live in
`st.session_state` (`app.py::load_data`), so they are silently lost and the app pays a
full GCS parquet reload.

```bash
gcloud compute backend-services update BACKEND_NAME --global \
  --session-affinity=GENERATED_COOKIE \
  --affinity-cookie-ttl=3600
```

## 3. Cloud Run: request timeout

**Default 300s → 3600s.** Same mechanism as #1 one layer down; caps the websocket at
5 minutes if left alone. Match it to the LB timeout.

```bash
gcloud run services update SERVICE_NAME --region REGION \
  --timeout=3600
```

## 4. Cloud Run: minimum instances

**Default 0 → 1.** Stops a reconnect from paying a cold start, which turns a ~1s blip
into several seconds.

```bash
gcloud run services update SERVICE_NAME --region REGION \
  --min-instances=1
```

---

## Optional — only if blips persist after the above

Streamlit's own ping defaults to a 30s interval with a 30s timeout. If a pong is delayed
past the timeout through IAP, the *server* closes the connection. Shorten the interval so
pings land well inside any intermediate idle threshold (Streamlit sets the ping timeout
equal to the interval).

`.streamlit/config.toml`:

```toml
[server]
websocketPingInterval = 20
```

Requires a rebuild and redeploy — unlike 1–4, this is a code change.

Related default worth knowing: `server.disconnectedSessionTTL` is 120s. A session whose
websocket stays down longer than that may be cleaned up server-side, edits included.

---

## Verifying

Before changing anything, confirm the cadence — it identifies the culprit:

- DevTools → Network → **WS** filter. Watch the connection close and reopen.
  **~30s** = LB backend timeout (#1). **~300s** = Cloud Run timeout (#3).
- Or in the Elements panel, watch `data-test-connection-state` on
  `[data-testid="stApp"]`; it flips off `CONNECTED` at each blip.

After the change, the WS connection should stay open indefinitely and the toolbar should
stop flashing.

## Reading current values

```bash
gcloud compute backend-services describe BACKEND_NAME --global \
  --format='value(timeoutSec,sessionAffinity,affinityCookieTtlSec)'

gcloud run services describe SERVICE_NAME --region REGION \
  --format='value(spec.template.spec.timeoutSeconds,spec.template.metadata.annotations)'
```

## Related app-side change

`app.py::inject_css` keeps Streamlit's status widget visible (pinned bottom-right) and
uses a faded-green disabled style instead of grey-with-a-dashed-border. That makes a
reconnect legible rather than alarming — it does **not** stop the reconnects. The
settings above are the actual fix.
