# CSV Data Editor (Streamlit)

Streamlit implementation of the IYS "Data Editor" wireframes: authenticated users edit cells of a tabular dataset in a grid, filter rows with live search, review pending changes with per-cell validation, and publish behind a confirmation dialog. Data can also be exported as a CSV snapshot or bulk-replaced by importing one.

Auth and storage are **pluggable providers selected in `config.yaml`** (see **[CONFIG.md](CONFIG.md)** for the full guide) — swap Google OAuth or a GCS/BigQuery-backed dataset for something else without touching application code. This repo ships two deployment shapes:

- **Zero-setup local dev** (the shipped default): `mock` auth + `local_csv` storage, backed by `sample_data/resources.csv`.
- **988 GCP deployment**: `google_oauth` auth + `gcs_parquet` storage (a parquet file on GCS, with a structured audit trail in BigQuery). See "GCP deployment" below.

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py
```

Log in with `admin` / `admin` (the dev `mock` auth provider). The default storage provider is `local_csv` backed by `sample_data/resources.csv` (96 sample crisis-line / community-resource rows, including required-blank cells to exercise the amber warnings), so the app runs end-to-end with no cloud setup at all.

**`config.yaml` ships with `auth.provider: mock` and `storage.provider: local_csv` on purpose**, even though this repo also contains the GCP-ready `google_oauth`/`gcs_parquet` providers — flipping those on requires real Google Cloud credentials and a live bucket/table, which would break `streamlit run app.py` (and the test suite) for anyone without that access. For the real 988 deployment, either edit `config.yaml`'s two `provider:` lines directly, or point `CSV_EDITOR_CONFIG` at a separate deployment-specific config file.

## Screens → wireframes

| Wireframe | Where |
|---|---|
| 1a Login | centered card, logo mark, email/password (`mock`/`gcloud_identity`) or a "Log in with Google" button (`google_oauth`); `gcp_iap` skips this screen entirely and signs the user in automatically |
| 1b Editing | toolbar (title, file info, search, Export Data, Import Data, "Review changes (n)" badge, avatar), grid |
| 2a Search | as-you-type filtering, blue filter bar (`Showing x of n rows matching "…"`), Clear search, yellow match highlighting; **edits on hidden rows are kept and the badge count is unchanged** |
| 1c Review | only edited rows, green-tinted changed cells showing `old → new`, cells still editable, summary "n rows · m cells changed", plus a diff panel with struck-through old values |
| 1d Validation | invalid cells get the red fill + outline + inline `✗ …` message; summary turns red ("k cells invalid"); Publish disabled (grey, dashed border) |
| 1e Publish | modal dialog with dynamic cell/row counts, Cancel / "Yes, publish" |
| — Import | file uploader (CSV only) → column-shape check → whole-file validation review (no path back to the editor except an explicit discard) → publish, gated on zero errors |

### Grid: a plain HTML table, not a Streamlit widget

Streamlit's `st.dataframe`/`st.data_editor` can't give every cell independent background color *and* real inline editing at the same time (`pandas.Styler` CSS is dropped on any column left editable). So the grid is rendered as a genuine `<table>` via `st.markdown` — full control over per-cell color (red error / amber warning / green edited / yellow search-match), with a small JS layer providing the interaction:

- **Double-click a cell** → a small edit box pops up positioned exactly over that cell (sized to the larger of the cell or its content), pre-filled with the value.
- **Tab / Enter / click away** commits the value into the cell; **Esc** discards and restores the original text.
- Since the table is static HTML (no Streamlit widget protocol), a committed edit is sent back to Python over a same-origin bridge: the JS writes the new value into a hidden `st.text_input` using the same trick browser devtools use to drive a React-controlled input (a native property setter + a real `input` event), then blurs it — that's what actually triggers the round-trip to the backend (`app.py::apply_bridge_edit`).

The same table/bridge machinery renders three different targets — the editing grid, the review grid, and the CSV-import review grid — distinguished by a `page` field carried through the bridge payload.

## Architecture

```
app.py                      Streamlit UI (login / editing / review / import / publish)
config.yaml                 provider selection + column & validation spec
core/
  config.py                 config loader, ColumnRule dataclass
  validation.py             type / length / regex validation engine
  audit.py                  change-log row builders (insert/update/delete, before/after)
  csv_import.py             CSV import: column-shape check + whole-file validation
  oauth_flow.py             OAuth redirect-callback decision logic (pure, unit-testable)
  publish.py                shared publish helper (audit/data write ordering)
providers/
  auth/
    base.py                 AuthProvider ABC + User (credential form OR redirect-based)
    mock.py                 dev users from config
    gcloud_identity.py      Google Cloud Identity Platform (email+password REST)
    google_oauth.py         Google OAuth, full authorization-code flow run by this app
    __init__.py             registry + factory (lazy imports)
  storage/
    base.py                 StorageProvider ABC (load / apply_edits / replace_all / write_audit)
    local_csv.py             local file, atomic writes
    bigquery.py              BigQuery table, atomic MERGE publish
    gcs_parquet.py            GCS parquet file + BigQuery change-log audit trail
    __init__.py              registry + factory (lazy imports)
tests/
  test_app.py                end-to-end view tests (Streamlit AppTest)
  test_validation.py         validation engine + audit write
  test_audit.py, test_csv_import.py, test_oauth_flow.py, test_publish.py,
  test_google_oauth.py, test_gcs_parquet.py, test_import_flow.py
                              unit tests for the new modules above (see "Tests")
  stubs/gcp_fakes.py         fake google.cloud.storage/bigquery + google.oauth2/auth,
                              so GCP-touching code is testable with zero real credentials
sample_data/
  resources.csv               local-dev dataset (local_csv default)
  dummy_988_data.csv          988-schema dummy data w/ 2 deliberately invalid rows,
                              for exercising Import validation — removable before launch
```

### State model
- `user` — session auth
- `original_df` — dataset from the storage provider, indexed by a stable `_row_id`
- `edits` — `{(row_id, column): new_value}`; reverting a cell to its original value removes the entry
- `import_df` / `import_edits` — an uploaded CSV pending review, and any corrections made on the import-review grid (separate from `edits` — importing doesn't touch the normal undo/redo history)
- `view` — `editing | review | import_upload | import_review`; publish dialogs via `st.dialog`
- Editor publish → provider `apply_edits()` → merge into `original_df`, clear `edits`
- Import publish → provider `replace_all()` → `original_df` becomes the imported data outright

## Swapping providers

Everything is driven by two lines in `config.yaml`:

```yaml
auth:
  provider: google_oauth   # or: mock | gcloud_identity | gcp_iap
storage:
  provider: gcs_parquet    # or: local_csv | bigquery
```

### Auth: Google OAuth (988 deployment)
`google_oauth` has the app itself run the full OAuth authorization-code flow — no fronting proxy involved. See [CONFIG.md](CONFIG.md#google_oauth--google-oauth-run-by-this-app-988-gcp-deployment) for the config block and [GCP deployment](#gcp-deployment) below for consent-screen/credentials setup.

### Auth: Google Cloud IAP (alternative to Google OAuth)
`gcp_iap` is for deployments that put the app behind an Identity-Aware Proxy load balancer instead: IAP handles Google sign-in and forwards a signed identity JWT, so the app never renders a login form. See [CONFIG.md](CONFIG.md#gcp_iap--google-cloud-identity-aware-proxy) for the config block and [IAP setup](#iap-setup-gcp_iap) below for the load-balancer/IAM steps.

### Storage: GCS parquet + BigQuery change log (988 deployment)
`gcs_parquet` reads/writes a single parquet object on GCS (in-memory only — never a local temp file) and logs a structured before/after trail to BigQuery on every publish. See [CONFIG.md](CONFIG.md#gcs_parquet--gcs-parquet-file--bigquery-change-log-988-gcp-deployment) for the config block.

### Adding a new provider
Implement the ABC, register it, name it in config — no other changes:

```python
# providers/storage/postgres.py
class PostgresStorageProvider(StorageProvider):
    name = "postgres"
    def load(self) -> pd.DataFrame: ...
    def apply_edits(self, df, edits) -> None: ...

# providers/storage/__init__.py
register_storage_provider("postgres",
    lambda: _import("providers.storage.postgres", "PostgresStorageProvider"))
```

The contract is documented in `providers/storage/base.py`: `load()` must return a DataFrame indexed by a stable unique row id; `apply_edits()` receives already-validated `{(row_id, column): value}` and should persist atomically where the backend allows; `replace_all()`/`supports_import` are optional (only needed to support the Import feature); `write_audit()`/`audit_before_data_write` are optional (only needed for an audit trail). Same pattern for auth in `providers/auth/` — `authenticate()` for a credential-form provider, `redirect_based = True` + `get_login_url()`/`complete_login()` for a redirect-based one (see `google_oauth.py`), or `header_based = True` + `authenticate_from_headers()` for a fronting-proxy one (see `gcp_iap.py`).

## Columns & validation

The dataset is the 17-column crisis-line / community-resource schema. Everything is config (`dataset.columns` in `config.yaml`); the `type` field is the single source of truth for BOTH the cell editor and validation — see [CONFIG.md](CONFIG.md#4-dataset--columns-editors-and-validation) for the full field reference and recipes.

**Two validation severities**: required-but-blank cells are flagged inline (amber ⚠ "required — currently blank") but do **not** block publish; format/type failures (enum, float range, regex) show red ✗ errors and **do** block publish. All text is whitespace-trimmed on commit. Header glyphs mark types: `*` required · `▾` dropdown · `¶` text area · `#.#` float.

CSV **import** runs the exact same rules against every cell of the whole uploaded file (`core.csv_import.validate_dataframe`) — not a diff, since there's nothing meaningful to diff a fresh import against.

## Export

The toolbar's "Export CSV" button re-fetches the dataset **fresh from the storage provider** (not the current session's in-progress edits — export reflects the latest *published* state), builds the CSV named `988-Export-{UTC timestamp}.csv`, and triggers the browser download immediately in the same click — via `st.iframe` embedding a small script that creates and auto-clicks a `data:` URI download link (rather than `st.download_button`, which would need a second click since its data has to be ready at render time, before the fetch happens).

## Import

"Import CSV" (disabled if the active storage provider doesn't support it — `local_csv` and `gcs_parquet` do, `bigquery` doesn't) opens a dedicated page:

1. **Upload** a CSV (`.csv` only — enforced by the file picker). If you have unpublished edits, you'll be asked to confirm discarding them first.
2. **Column check**: the file's columns must exactly match the 17 configured columns — both missing and unexpected columns are rejected, listed by name in plain language.
3. **Validation**: every cell of the whole file is checked against the same rules used everywhere else in the app. The review grid (the same double-click-to-edit grid as the editor) lets you fix small issues inline before publishing, without needing to re-upload.
4. **Publish**: only enabled once there are zero validation errors. There's no path back to the normal editor from this flow except an explicit "Discard import" — publishing here **fully replaces the dataset**, not a diff/merge against what's there today.

## Audit / change log

Every publish (an editor cell-edit publish, or an import full-replace publish) builds change-log rows via `core/audit.py` and hands them to `StorageProvider.write_audit(metadata, records)`:

- **Update** (normal cell edits): edits are grouped **by row** first — a row with 3 edited cells produces one before/after pair, not three. Two rows are appended sharing one `change_id`: `change_state="before"` (the row's full original values) and `change_state="after"` (its full new values).
- **Insert** (CSV import): one row per imported row, `change_state="after"` only — there's no "before" for a fresh import.
- **Delete**: `core/audit.py::delete_row` exists for schema completeness (matching the change-log table's `change_type` values) but nothing in the UI triggers it — there's no row-delete feature in this app.

`local_csv` appends JSON lines to `<path>.audit.jsonl`; `bigquery` inserts into `storage.bigquery.audit_table` if configured; `gcs_parquet` inserts into its `change_log` table (required, not optional, for that provider). If the audit write fails the publish still stands and the app shows a non-blocking warning — **except** for `gcs_parquet`, which writes the change log **before** the parquet write (`audit_before_data_write = True`, the opposite order from the other two providers). That's a deliberate trade-off: if the parquet write then fails, the change log has an entry for something that never landed — the safer failure mode for a compliance log than data changing with no record of it at all — and the app surfaces that specific situation as its own distinct blocking error rather than a silent warning, since at that point a human needs to look.

## GCP deployment

This section covers the `google_oauth` + `gcs_parquet` provider pair specifically.

### Secrets / environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | `google_oauth` auth | OAuth 2.0 client ID (Google Cloud Console → Credentials) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | `google_oauth` auth | OAuth 2.0 client secret |
| `IAP_AUDIENCE` | `gcp_iap` auth | Expected JWT audience, `/projects/PROJECT_NUMBER/global/backendServices/SERVICE_ID` — see [IAP setup](#iap-setup-gcp_iap) below |
| `GOOGLE_APPLICATION_CREDENTIALS` | `gcs_parquet` storage | Path to a service account key JSON — **omit this entirely** if running on GCP infrastructure that already provides Application Default Credentials (Cloud Run's attached runtime service account, GCE metadata server, etc.); only needed for local development against real GCP resources. |

`config.yaml`'s `auth.google_oauth.redirect_uri` (not a secret, but deployment-specific) must exactly match an authorized redirect URI on the OAuth client.

### IAM roles (for the runtime service account)

- **`roles/storage.objectAdmin`** scoped to the `collab-nprod-data` bucket (or a narrower `objectViewer` + `objectCreator` combo if you don't want delete permission — this app never deletes the parquet object, only overwrites it).
- **`roles/bigquery.dataEditor`** scoped to the `collaborator_988_raw` dataset in `collab-infra-nprod`, for the `988_change_log` table. **`roles/bigquery.jobUser` is not required** for this — the change-log write is a streaming insert (`insert_rows_json` / `tabledata.insertAll`), not a query job. (This is unlike the separate, generic `bigquery` *storage* provider in this repo, which does run real queries and does need `jobUser` if you use it instead.)

### OAuth consent screen

1. Google Cloud Console → APIs & Services → OAuth consent screen. Choose **Internal** if every user is in your Google Workspace org, else **External**.
2. Scopes: `openid`, `email`, `profile` only — Google's non-sensitive bucket, so no app-verification review is required even for an External screen.
3. Credentials → Create OAuth client ID (Web application). Add the deployed app's URL as an authorized redirect URI, exactly matching `redirect_uri` in `config.yaml`.

### IAP setup (`gcp_iap`)

Only needed if using `gcp_iap` instead of `google_oauth` — IAP replaces the OAuth consent screen/client above entirely.

1. Deploy Cloud Run with public access off and ingress locked to the load balancer, so IAP can't be bypassed by hitting the service directly:
   ```bash
   gcloud run deploy 988-data-editor --source . --region us-central1 \
     --no-allow-unauthenticated --ingress=internal-and-cloud-load-balancing
   ```
2. Put an external HTTPS Application Load Balancer with a serverless NEG in front of that Cloud Run service.
3. Console → Security → Identity-Aware Proxy → enable IAP on that backend service.
4. Grant `roles/iap.httpsResourceAccessor` ("IAP-secured Web App User"), scoped to the backend service, to the users/group who should have access.
5. Get the audience string IAP signs the JWT for:
   ```bash
   PROJECT_NUMBER=$(gcloud projects describe PROJECT_ID --format='value(projectNumber)')
   BACKEND_SERVICE_ID=$(gcloud compute backend-services describe SERVICE_NAME --global --format='value(id)')
   echo "/projects/$PROJECT_NUMBER/global/backendServices/$BACKEND_SERVICE_ID"
   ```
6. Set `IAP_AUDIENCE` to that string, set `auth.provider: gcp_iap` in `config.yaml`, redeploy.

### Cloud Run deploy (example)

```bash
gcloud run deploy 988-data-editor \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --session-affinity \
  --set-env-vars GOOGLE_OAUTH_CLIENT_ID=...,GOOGLE_OAUTH_CLIENT_SECRET=...
```

**`--session-affinity` is required, not optional**, if the service can scale beyond one instance. The OAuth CSRF `state` value round-trips through `st.session_state`, which lives in-process on whichever instance handled the initial "Log in with Google" click; without session affinity, the redirect-back can land on a different instance and every login will spuriously fail the CSRF check.

Before flipping `config.yaml`'s `provider:` lines to `google_oauth`/`gcs_parquet` for a real deployment, do a one-time manual check that the real parquet file's and `988_change_log` table's column names match `config.yaml`'s 17 `name:` values exactly — a mismatch surfaces as a `StorageError` on first real read/write, not at config-load time.

## Keyboard shortcuts

Ctrl/Cmd+Z = undo · Ctrl/Cmd+Shift+Z or Ctrl+Y = redo (suppressed while a cell editor has focus so native text-undo still works).

## Tests

```bash
python -m pytest tests/ -q
```

Every test is a unit test (or an `AppTest` run against `mock`/`local_csv` — the app's zero-setup defaults). Nothing hits real GCP, real network, or real Google OAuth:

- `test_validation.py`, `test_audit.py`, `test_csv_import.py`, `test_oauth_flow.py`, `test_publish.py` — pure logic, no I/O.
- `test_google_oauth.py`, `test_gcs_parquet.py` — exercise the real provider code against `tests/stubs/gcp_fakes.py`, which installs fake `google.cloud.storage`/`google.cloud.bigquery`/`google.oauth2.id_token`/`google.auth.transport.requests` modules (`google-cloud-storage`/`google-cloud-bigquery`/`google-auth` are optional dependencies — these tests pass with none of them installed).
- `test_app.py`, `test_import_flow.py` — `AppTest`-driven UI flows against the real `mock`/`local_csv` providers.

`sample_data/dummy_988_data.csv` (17-column schema, 2 deliberately-invalid rows) exercises the Import feature's validation surfacing in both manual testing and `test_import_flow.py` — it's dev/test fixture data, safe to delete before launch.

## Notes & known trade-offs
- Values are edited as strings and normalized by the storage provider on publish; typed BigQuery columns need the `CAST` noted in CONFIG.md's `bigquery` section once a typed schema is in use.
- Search-match highlighting tints the whole matching cell (not just the matched substring).
- Concurrent editors aren't coordinated: last publish wins. For `gcs_parquet` specifically this is a bigger blast radius than the other providers — a full-object rewrite, not a per-cell `MERGE` — see [CONFIG.md](CONFIG.md#gcs_parquet--gcs-parquet-file--bigquery-change-log-988-gcp-deployment) for the known limitation and a possible future hardening.
- There is no row-delete feature anywhere in the app; the change-log schema supports `change_type="delete"` for completeness, but nothing produces one.
