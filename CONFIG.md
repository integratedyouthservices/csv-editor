# Configuration Guide — `config.yaml`

Everything configurable about the CSV Data Editor lives in one file: `config.yaml`, next to `app.py`. This guide covers each section: what the keys mean, what values are allowed, and what to edit for the common tasks (switching auth, switching storage, changing columns/validation).

You can also point the app at a different config file entirely by setting the environment variable `CSV_EDITOR_CONFIG=/path/to/other-config.yaml` — useful for keeping a dev config and a prod config side by side.

**After any config change, restart the app** (or press "Rerun"/`R` in Streamlit) — the config is cached for the session.

---

## File layout at a glance

```yaml
app:        # titles shown in the UI
auth:       # WHICH identity provider + its settings
storage:    # WHERE the data lives + its settings
dataset:    # display name + the 17 column definitions (editors + validation)
```

---

## 1. `app` — UI text

```yaml
app:
  title: "Data Editor"                      # toolbar + landing card title
  subtitle: "sign in to edit the dataset"   # landing card subtitle
```

Cosmetic only; safe to change anytime.

---

## 2. `auth` — authentication

**This app has no login form.** It never sees a password, holds no user list, and cannot be signed into on its own — identity arrives as a signed header from Identity-Aware Proxy, and `iap` is the only registered auth provider:

```yaml
auth:
  provider: iap              # the only registered value
```

### `iap` — Identity-Aware Proxy (the only provider)

Trusts Google Cloud IAP: the app must be deployed behind it, and every request already carries a signed identity assertion (`X-Goog-IAP-JWT-Assertion`) IAP verifies and attaches before the request reaches Streamlit. `authenticate_from_headers()` verifies the token's signature and audience (via `google-auth`, against Google's published JWKs) and reads the `email` claim.

```yaml
auth:
  provider: iap
  iap:
    audience_env: IAP_AUDIENCE   # name of the env var holding the audience string
    # audience: "..."            # inline fallback — avoid committing this
    login_url_env: IAP_LOGIN_URL # optional — see "The landing page" below
    # login_url: "https://<your-iap-protected-host>/"
```

```bash
# Windows (PowerShell)
$env:IAP_AUDIENCE = "/projects/PROJECT_NUMBER/global/backendServices/SERVICE_ID"
# Linux/macOS
export IAP_AUDIENCE="/projects/PROJECT_NUMBER/global/backendServices/SERVICE_ID"
```

- The audience format depends on the backend: `/projects/PROJECT_NUMBER/global/backendServices/SERVICE_ID` for an external HTTPS load balancer, `/projects/PROJECT_NUMBER/apps/PROJECT_ID` for App Engine. See https://cloud.google.com/iap/docs/signed-headers-howto.
- **This is app-level defense in depth, not the access-control gate** — the actual gate is the **IAP-Secured Web App User** IAM role (`roles/iap.httpsResourceAccessor`), granted per user/group on the backend service in the Cloud Console. The app trusts whoever IAP already let through; it doesn't manage its own user list.
- The backend the app runs on (e.g. Cloud Run) must **not** be reachable except through the IAP-fronted load balancer, or the header can be spoofed by anyone hitting it directly. See the README's "GCP deployment" section.
- The verified email is what gets written to `changed_by` in the change log and `last_updated_by` in the audit metadata.
- Requires `pip install google-auth` (already in `requirements.txt`).

### The landing page

A request with a valid assertion goes straight to the editor — no click, no screen. A request without one gets a landing page carrying two navigations (not Streamlit buttons: a rerun replays the script over the existing websocket and can never pick up a fresh header — only a real browser navigation can, and that navigation is what IAP intercepts):

| Button | Goes to |
|---|---|
| **Log in** | `login_url` — the IAP-protected URL, which starts IAP's Google sign-in flow |
| **Sign in as a different user** | `login_url` + `?gcp-iap-mode=CLEAR_LOGIN_COOKIE` — clears IAP's login cookie first, for a stale or wrong-account session. Same URL behind **Sign out** in the avatar popover. See https://cloud.google.com/iap/docs/sessions-howto |

`login_url` defaults to `/`, which is correct whenever the app is only ever reachable through IAP — the browser is already on the protected host, so re-navigating re-enters sign-in. Set `IAP_LOGIN_URL` (or `auth.iap.login_url`) to the load balancer hostname only if a browser can land on the app some other way; `/` would loop there.

A misconfiguration (missing audience) or an untrustworthy assertion shows its message on the same landing page rather than a blank error, so the Log in / Sign in as a different user buttons stay reachable.

### Local development

With `iap` the app stops at the landing page on a workstation — there's no IAP in front, so there's no identity and no form to fill in. To reach the editor locally, set `st.session_state.user` yourself (a `providers.auth.base.User`), e.g. via `streamlit.testing.v1.AppTest`, or run behind a real IAP deployment.

### Adding a new auth provider (e.g. Okta, Azure AD)

No config-only path — it's ~20 lines of code, then config:

1. Create `providers/auth/okta.py` subclassing `AuthProvider` (see `providers/auth/base.py` for the contract: `authenticate_from_headers(headers)` returns a `User`, `None` when the request carries no identity at all, and raises `AuthError` when one is present but untrustworthy; `login_url()`/`restart_login_url()` say where the landing page's buttons point).
2. Register it in `providers/auth/__init__.py`:
   ```python
   _REGISTRY["okta"] = lambda: _import("providers.auth.okta", "OktaAuthProvider")
   ```
3. Add its settings block and set `auth.provider: okta` in `config.yaml`.

The base class deliberately exposes **no** username/password or redirect-callback hook. A provider needing either would also need a login screen added back to `app.py`, which this branch does not have.

---

## 3. `storage` — where the data lives

### Switching providers

```yaml
storage:
  provider: gcs_parquet      # ← change this: gcs_parquet | local_csv | bigquery
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
```

If the app shows "Unknown auth provider" / "Unknown storage provider" on start, the `provider:` value doesn't match a registered name — the error message lists the available ones.
