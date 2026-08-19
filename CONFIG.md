# Configuration Guide — `config.yaml`

Everything configurable about the CSV Data Editor lives in one file: `config.yaml`, next to `app.py`. This guide covers each section: what the keys mean, what values are allowed, and what to edit for the common tasks (switching auth, switching storage, changing columns/validation).

You can also point the app at a different config file entirely by setting the environment variable `CSV_EDITOR_CONFIG=/path/to/other-config.yaml` — useful for keeping a dev config and a prod config side by side.

**After any config change, restart the app** (or press "Rerun"/`R` in Streamlit) — the config is cached for the session.

---

## File layout at a glance

```yaml
app:        # titles shown in the UI
auth:       # WHICH login provider + its settings
storage:    # WHERE the data lives + its settings
dataset:    # display name + the 17 column definitions (editors + validation)
```

---

## 1. `app` — UI text

```yaml
app:
  title: "Data Editor"                      # toolbar + login card title
  subtitle: "sign in to edit the dataset"   # login card subtitle
```

Cosmetic only; safe to change anytime.

---

## 2. `auth` — authentication

### Switching providers

One line selects the provider; each provider then reads only its own settings block (unused blocks can stay in the file as documentation):

```yaml
auth:
  provider: mock            # ← change this: mock | gcloud_identity | google_oauth | gcp_iap
```

### `mock` — development only

```yaml
auth:
  provider: mock
  mock:
    users:                  # plain username: password pairs
      admin: "admin"
      editor: "editor"
      yourname: "yourpassword"
```

Add/remove users by editing the `users:` map. **Never use this in production** — passwords are plain text in the file.

### `gcloud_identity` — Google Cloud Identity Platform (current production target)

Email + password sign-in via the Identity Toolkit REST API.

```yaml
auth:
  provider: gcloud_identity
  gcloud_identity:
    api_key_env: GCP_IDENTITY_API_KEY   # name of the env var holding the API key
    # api_key: "AIza..."                # inline fallback — avoid committing this
```

Then set the key in the environment before launching:

```bash
# Windows (PowerShell)
$env:GCP_IDENTITY_API_KEY = "AIza..."
# Linux/macOS
export GCP_IDENTITY_API_KEY="AIza..."
```

The API key comes from your Google Cloud project (Identity Platform → Application setup details). Users must exist in Identity Platform with email/password sign-in enabled. Wrong credentials show "Incorrect email or password"; a missing key or network problem shows a separate "Sign-in is unavailable" message so you can tell configuration issues apart from bad passwords.

**Note:** the signed-in email is what gets written to `last_updated_by` in the audit metadata, so production should use real emails via `gcloud_identity`.

### `google_oauth` — Google OAuth, run by this app (988 GCP deployment)

The app itself performs the full OAuth authorization-code flow: it renders a "Log in with Google" link, Google redirects the browser back with `?code&state`, and the app exchanges the code for an ID token directly. This is **not** the "trust a header from an external proxy" pattern — there is no fronting proxy involved. If the app instead sits behind an Identity-Aware Proxy load balancer, use `gcp_iap` below.

```yaml
auth:
  provider: google_oauth
  google_oauth:
    client_id_env: GOOGLE_OAUTH_CLIENT_ID
    client_secret_env: GOOGLE_OAUTH_CLIENT_SECRET
    # client_id / client_secret: "..."     # inline fallback — avoid committing
    redirect_uri: https://<your-cloud-run-service>/
```

```bash
pip install google-auth
export GOOGLE_OAUTH_CLIENT_ID="....apps.googleusercontent.com"
export GOOGLE_OAUTH_CLIENT_SECRET="...."
```

- `redirect_uri` must **exactly** match an authorized redirect URI configured on the OAuth client in Google Cloud Console (Credentials → OAuth 2.0 Client IDs).
- Requires an OAuth consent screen (Internal, for a Workspace-only deployment, or External). `openid`/`email`/`profile` are Google's non-sensitive scope bucket, so no app-verification review is required even for an External consent screen.
- A rejected/cancelled login and a CSRF `state` mismatch both show a plain-language error and return to the login button; they don't crash the app.
- **Multi-replica deployments (Cloud Run) must enable session affinity.** The CSRF `state` value round-trips through `st.session_state`, which lives in-process on whichever server instance handled the initial click; if the redirect-back lands on a *different* instance without session affinity, login fails unpredictably.
- See the README's "GCP deployment" section for the end-to-end setup (consent screen, credentials, IAM, deploy).

### `gcp_iap` — Google Cloud Identity-Aware Proxy

For deployments that put IAP in front of the app (e.g. Cloud Run behind an external HTTPS load balancer with IAP enabled). IAP itself handles the Google sign-in; the app never renders a login form — it just reads and verifies the signed identity JWT (`X-Goog-IAP-JWT-Assertion`) IAP adds to every request. This *is* the "trust a header from an external proxy" pattern `google_oauth` above explicitly isn't — safe here only because the JWT is cryptographically verified against Google's IAP-specific public keys and a specific audience, not merely read off the header.

```yaml
auth:
  provider: gcp_iap
  gcp_iap:
    audience_env: IAP_AUDIENCE
    # audience: "..."   # inline fallback — avoid committing
```

```bash
pip install google-auth
export IAP_AUDIENCE="/projects/PROJECT_NUMBER/global/backendServices/SERVICE_ID"
```

- `audience` must match exactly what IAP signs the JWT for. For the typical Cloud Run + external HTTPS load balancer setup this is `/projects/PROJECT_NUMBER/global/backendServices/SERVICE_ID` — see [Securing your app with signed headers](https://cloud.google.com/iap/docs/signed-headers-howto#verifying_the_jwt_payload) for how to look up the project number and backend service ID.
- **The app must actually be unreachable except through IAP.** This provider only trusts the signed JWT (never the plaintext `X-Goog-Authenticated-User-*` headers IAP also sets), but that protection is void if the backend can be reached directly, bypassing the load balancer — e.g. a Cloud Run service with public ingress. Restrict ingress to the load balancer (`--ingress=internal-and-cloud-load-balancing` for Cloud Run) so IAP can't be routed around.
- "Log out" isn't meaningful from inside the app — IAP re-authenticates from the header on every request, so clearing the app's own session just logs the same identity straight back in. The avatar menu instead links to IAP's own [`/_gcp_iap/clear_login_cookie`](https://cloud.google.com/iap/docs/faq) endpoint, which is IAP's supported way to end the session (e.g. to switch Google accounts).
- IAP is set up at the load balancer / IAM level (enabling IAP, restricting ingress, granting access), not in this app — see the README's ["IAP setup"](README.md#iap-setup-gcp_iap) for the ordered steps.

### Adding a new auth provider (e.g. Okta, Azure AD)

No config-only path — it's ~20 lines of code, then config:

1. Create `providers/auth/okta.py` subclassing `AuthProvider` (see `providers/auth/base.py` for the contract: `authenticate(username, password)` returns a `User` on success, `None` on bad credentials, raises `AuthError` for infrastructure failures).
2. Register it in `providers/auth/__init__.py`:
   ```python
   _REGISTRY["okta"] = lambda: _import("providers.auth.okta", "OktaAuthProvider")
   ```
3. Add its settings block and set `auth.provider: okta` in `config.yaml`.

---

## 3. `storage` — where the data lives

### Switching providers

```yaml
storage:
  provider: local_csv       # ← change this: local_csv | bigquery | gcs_parquet
```

### `local_csv` — local file (development / fallback)

```yaml
storage:
  provider: local_csv
  local_csv:
    path: sample_data/resources.csv   # relative to the project root, or absolute
    id_column: null                   # null → row order is the row id
```

- `path`: which CSV to edit. Publishing writes back to this file atomically.
- `id_column`: if your CSV has a stable unique key column (e.g. `id`), name it here; edits are then keyed by that value instead of row position. Must be unique — duplicates fail the load with a clear error.
- Audit trail: each publish appends one JSON line to `<path>.audit.jsonl` next to the CSV (who, when, and every changed cell). No config needed.

### `bigquery` — Google BigQuery (current production target)

```yaml
storage:
  provider: bigquery
  bigquery:
    project: my-gcp-project      # GCP project id
    dataset: my_dataset          # BigQuery dataset
    table: resources             # table being edited
    id_column: id                # REQUIRED — stable unique key column
    audit_table: resources_audit # optional — audit rows go here; omit to skip
    location: US                 # dataset location
```

- Requires `pip install google-cloud-bigquery db-dtypes`.
- Credentials come from Application Default Credentials, not from this file:
  ```bash
  gcloud auth application-default login
  # or a service account:
  export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
  ```
- `id_column` is mandatory — publishing runs a single `MERGE` keyed on it, so all cells land atomically or not at all.
- `audit_table`: if present, the post-publish audit write inserts one row per changed cell (`row_id, column, old_value, new_value, timestamp, user, last_updated_at, last_updated_by`). Create the table with those STRING columns. If the audit insert fails the publish still stands; the app shows a non-blocking warning. Omit the key entirely to disable the audit write.
- Current limitation: edited values are bound as STRING parameters. If your table has typed columns (FLOAT64, etc.), add per-column `CAST`s in `providers/storage/bigquery.py` (the spot is marked at the `SET` clause builder).

### `gcs_parquet` — GCS parquet file + BigQuery change log (988 GCP deployment)

The dataset lives as a single parquet object on Google Cloud Storage; every publish (edit or full CSV import) also appends a structured before/after audit trail to a separate BigQuery table.

```yaml
storage:
  provider: gcs_parquet
  gcs_parquet:
    bucket: collab-nprod-data
    blob_path: collaborator_988_raw/raw_crisis_988_data/geo_coded_988_data.parquet
    id_column: null           # null → row order is the row id
    change_log:
      project: collab-infra-nprod
      dataset: collaborator_988_raw
      table: 988_change_log
      location: US
```

```bash
pip install google-cloud-storage google-cloud-bigquery pyarrow
gcloud auth application-default login   # or GOOGLE_APPLICATION_CREDENTIALS
```

- Every read/write moves through an in-memory buffer only (`blob.download_as_bytes()` / `blob.upload_from_string()`) — never a local temp file, so there's nothing left open or half-written if something fails partway through. A GCS object write is a single atomic PUT: readers never see a partial object.
- `change_log` is **required** — the table must already exist with the dataset's 17 data columns plus `change_id`, `change_state` (`before`/`after`), `change_type` (`insert`/`update`/`delete`), `changed_by`, `changed_at`. See the README's "Audit / change log" section for exactly how rows are built.
- **Write order is audit-first**, unlike `local_csv`/`bigquery` above: the change-log write happens *before* the parquet write on every publish. If the parquet write then fails, the app surfaces a distinct blocking error rather than silently leaving the data unwritten while the log says otherwise — see the README for why this ordering was chosen.
- Supports the app's CSV **Import** feature (`supports_import = True`): an imported file fully replaces the parquet object; every row is logged as a fresh `insert`.
- **Known limitation**: publishing rewrites the entire parquet object from an in-memory DataFrame, so two concurrent editors can clobber each other's *unrelated* changes (a bigger blast radius than `bigquery`'s targeted per-cell `MERGE` above). Not hardened in this version — a future enhancement would use GCS generation preconditions (`if_generation_match`) to fail loudly instead of clobbering.

### Adding a new storage provider (e.g. Postgres, S3)

Same pattern as auth: subclass `StorageProvider` (`providers/storage/base.py` documents the contract — `load()` returns a DataFrame indexed by a stable unique row id; `apply_edits()` persists `{(row_id, column): value}`; `write_audit()` and `replace_all()` are optional, as are the `audit_before_data_write`/`supports_import` class attributes that control ordering and gate the Import feature), register it in `providers/storage/__init__.py`, add a settings block, point `storage.provider` at it.

---

## 4. `dataset` — columns, editors, and validation

```yaml
dataset:
  display_name: resources.csv   # shown in the toolbar and publish dialog
  columns:                      # ORDER HERE = column order in the grid
    - name: service_category
      ...
```

Each entry in `columns:` defines one column. **`type` is the single source of truth**: it decides which editor the grid opens for that cell AND how the value is validated.

### Fields per column

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Column name in the CSV/table. Must match the data exactly. |
| `label` | no | Header text (defaults to `name` uppercased). The app appends type glyphs automatically: `*` required · `▾` dropdown · `¶` text area · `#.#` float. |
| `type` | no | `text` (default) · `textarea` · `enum` · `float`. Legacy `string`/`number`/`integer`/`date` also still work. |
| `required` | no | `true` → blank cells are flagged amber ("required — currently blank"). **Warnings do NOT block publish** — only format/type errors do. |
| `options` | enum only | The fixed list of allowed values; the grid shows them as a dropdown, anything else is a red error. |
| `min` / `max` | float only | Numeric bounds. Out-of-range → red error "must be a number, −90 to 90". |
| `regex` | no | Full-match regular expression (applies after trimming). Failure → red error. |
| `regex_hint` | no | The error message shown on regex failure (e.g. "must match A1A 1A1"). Without it a generic message is used. |
| `normalize` | no | Value normalizer applied on commit. Currently: `postal_code` (trim, uppercase, insert the internal space: `k1a0b1` → `K1A 0B1`). All text is whitespace-trimmed regardless. |
| `max_length` | no | Character cap; over → red error. |
| `editable` | no | `false` → column is read-only in the grid. |

### The two validation severities (important)

- **Amber warning** — a `required` column left blank. Shown inline in review (⚠), counted in the summary ("· 2 required cells blank"), but **publish stays enabled**. This is deliberate: the source data has blanks today (e.g. 11 blank `phone_1`).
- **Red error** — any format/type failure: value not in an enum's `options`, float out of `min`/`max` or not numeric, `regex` mismatch, over `max_length`. **Any red error disables Publish.**

### Recipes

**Add a value to a dropdown** (e.g. a 10th service category):
```yaml
- name: service_category
  type: enum
  options:
    - "211 and Other Resource Databases"
    # ... existing 8 ...
    - "New Category Name"        # ← just add the line
```

**Make an optional column required** (flags blanks, doesn't block):
```yaml
- name: website
  required: true
```

**Add a format rule** to a free-text column:
```yaml
- name: phone_1
  regex: "^[0-9()+\\- ]+$"
  regex_hint: "digits, spaces, and ()+- only"
```
(Not currently done for phones on purpose — the data has mixed formats.)

**Add a brand-new column**: add it to the CSV/table first, then append an entry to `columns:` in the position you want it to appear. Columns present in the data but missing from `columns:` won't get an editor config or validation.

**Change validation without touching code**: every rule above is read at load time — edit YAML, restart, done. The same rules drive the editing grid, the review screen, and publish gating, so they can't drift apart.

---

## Quick sanity check after editing

```bash
python -c "import sys; sys.path.insert(0,'.'); from core.config import load_config; c = load_config('config.yaml'); print(c.auth_provider_name, c.storage_provider_name, len(c.columns), 'columns')"
python -m pytest tests/ -q     # validation tests will catch broken rules
```

If the app shows "Unknown auth provider" / "Unknown storage provider" on start, the `provider:` value doesn't match a registered name — the error message lists the available ones.
