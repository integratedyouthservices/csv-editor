# CSV Data Editor (Streamlit)

Streamlit implementation of the IYS "Data Editor" wireframes: authenticated users edit cells of a tabular dataset in a grid, filter rows with live search, review pending changes with per-cell validation, and publish behind a confirmation dialog. Data can also be exported as a CSV snapshot or bulk-replaced by importing one.

Auth and storage are **pluggable providers selected in `config.yaml`** (see **[CONFIG.md](CONFIG.md)** for the full guide) — swap the auth or storage backend for something else without touching application code. `config.yaml` ships configured for the **988 GCP deployment**: `iap` auth (the app trusts Google Cloud Identity-Aware Proxy) + `gcs_parquet` storage (a parquet file on GCS, with a structured audit trail in BigQuery). See "GCP deployment" below.

**`iap` is the only auth provider.** The app has no login form and never handles credentials — there is nothing to sign in *to*, only a landing page whose "Log in" button hands the browser to IAP. `local_csv` remains available as a local-dev storage fallback (backed by `sample_data/resources.csv`) — point `CSV_EDITOR_CONFIG` at a separate config file to use it without touching the shipped `config.yaml`. See [CONFIG.md](CONFIG.md) for every provider's settings.

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py
```

Auth is `iap`, so a request that doesn't come through Identity-Aware Proxy carries no identity and lands on the sign-in page — locally that means the app stops there. To exercise the editor on a workstation, run it behind IAP, or inject an identity yourself (see [CONFIG.md](CONFIG.md#2-auth--authentication) for the setup and the local-dev note).

## Screens → wireframes

| Wireframe | Where |
|---|---|
| 1a Login | replaced by a landing page: centered card + logo mark, a "Log in" button that navigates to the IAP-protected URL (starting IAP's own Google sign-in), and a "Sign in as a different user" escape hatch. There is no credential form — a request that already carries a valid IAP assertion skips this screen entirely |
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
app.py                      Streamlit UI (landing / editing / review / import / publish)
config.yaml                 provider selection + column & validation spec
core/
  config.py                 config loader, ColumnRule dataclass
  validation.py             type / length / regex validation engine
  audit.py                  change-log row builders (insert/update/delete, before/after)
  csv_import.py             CSV import: column-shape check + whole-file validation
  publish.py                shared publish helper (audit/data write ordering)
providers/
  auth/
    base.py                 AuthProvider ABC + User (header-based only — no credential entry point)
    iap.py                   Google Cloud Identity-Aware Proxy (trusts the signed request header)
    __init__.py             registry + factory (lazy imports)
  storage/
    base.py                 StorageProvider ABC (load / apply_edits / replace_all / write_audit)
    local_csv.py             local file, atomic writes (local-dev fallback)
    bigquery.py              BigQuery table, atomic MERGE publish
    gcs_parquet.py            GCS parquet file + BigQuery change-log audit trail
    __init__.py              registry + factory (lazy imports)
sample_data/
  resources.csv               local-dev dataset (local_csv fallback)
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
  provider: iap             # the only registered auth provider
storage:
  provider: gcs_parquet     # or: local_csv | bigquery
```

### Auth: Identity-Aware Proxy (the only provider)
`iap` trusts Google Cloud IAP: the app must be deployed behind it (Cloud Run, App Engine, or GCE/GKE with an IAP-protected backend), and every request already carries a signed identity assertion IAP verifies before the request reaches Streamlit — no login form, no credentials handled by the app itself, and no in-app user list. A request without a verified identity gets the landing page and its "Log in" button, which navigates to the IAP-protected URL so IAP can run sign-in. See [CONFIG.md](CONFIG.md#iap--identity-aware-proxy-the-only-provider) for the config block.

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

The contract is documented in `providers/storage/base.py`: `load()` must return a DataFrame indexed by a stable unique row id; `apply_edits()` receives already-validated `{(row_id, column): value}` and should persist atomically where the backend allows; `replace_all()`/`supports_import` are optional (only needed to support the Import feature); `write_audit()`/`audit_before_data_write` are optional (only needed for an audit trail). Same pattern for auth in `providers/auth/`, with one deliberate restriction: `AuthProvider` exposes **no credential entry point at all**, only `authenticate_from_headers()` for a proxy-trust provider (see `iap.py`), plus `login_url()`/`restart_login_url()` for where the landing page's buttons send the browser. Adding a username/password or app-run-OAuth provider would mean re-adding a login form to `app.py`, which this branch intentionally does not have.

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

This section covers the `iap` + `gcs_parquet` provider pair. IAP isn't optional here — it is the only way a user can get an identity into the app.

### Secrets / environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `IAP_AUDIENCE` | `iap` auth | Expected audience of the IAP-signed identity token — `/projects/PROJECT_NUMBER/global/backendServices/SERVICE_ID` (external HTTPS load balancer) or `/projects/PROJECT_NUMBER/apps/PROJECT_ID` (App Engine). See [CONFIG.md](CONFIG.md#iap--identity-aware-proxy-the-only-provider). |
| `IAP_LOGIN_URL` | `iap` auth | Optional. Where the landing page's "Log in" button sends the browser; must be the IAP-protected URL. Defaults to `/`, which is correct when the app is only ever reachable through IAP. |
| `GOOGLE_APPLICATION_CREDENTIALS` | `gcs_parquet` storage | Path to a service account key JSON — **omit this entirely** if running on GCP infrastructure that already provides Application Default Credentials (Cloud Run's attached runtime service account, GCE metadata server, etc.); only needed for local development against real GCP resources. |

### IAM roles (for the runtime service account)

- **`roles/storage.objectAdmin`** scoped to the `collab-nprod-data` bucket (or a narrower `objectViewer` + `objectCreator` combo if you don't want delete permission — this app never deletes the parquet object, only overwrites it).
- **`roles/bigquery.dataEditor`** scoped to the `collaborator_988_raw` dataset in `collab-infra-nprod`, for the `988_change_log` table. **`roles/bigquery.jobUser` is not required** for this — the change-log write is a streaming insert (`insert_rows_json` / `tabledata.insertAll`), not a query job. (This is unlike the separate, generic `bigquery` *storage* provider in this repo, which does run real queries and does need `jobUser` if you use it instead.)

### Enabling IAP (required — it is the only auth)

1. Deploy the app behind an HTTPS load balancer (Cloud Run works via a [serverless NEG](https://cloud.google.com/run/docs/mapping-custom-domains) backend) and enable IAP on that backend service under Security → Identity-Aware Proxy in the Cloud Console.
2. Grant each user/group the **IAP-Secured Web App User** role (`roles/iap.httpsResourceAccessor`) on the backend service — this is the actual access-control gate, separate from anything in this app.
3. Set `IAP_AUDIENCE` to that backend service's audience string (shown on the IAP console page, or via `gcloud iap web get-iam-policy`) — format `/projects/PROJECT_NUMBER/global/backendServices/SERVICE_ID`.
4. The Cloud Run service itself must **not** be publicly invokable — grant `roles/run.invoker` only to the load balancer's service agent, not `allUsers`, so requests can't bypass IAP by hitting the Cloud Run URL directly.

Full walkthrough: https://cloud.google.com/iap/docs/enabling-cloud-run

### Cloud Run deploy (example)

```bash
gcloud run deploy 988-data-editor \
  --source . \
  --region us-central1 \
  --no-allow-unauthenticated \
  --set-env-vars IAP_AUDIENCE=/projects/PROJECT_NUMBER/global/backendServices/SERVICE_ID
```

Then wire the service to an external HTTPS load balancer with IAP enabled, per the link above — `--no-allow-unauthenticated` alone doesn't set up IAP, it just ensures the Cloud Run URL can't be hit directly outside of it.

Before deploying for real, do a one-time manual check that the real parquet file's and `988_change_log` table's column names match `config.yaml`'s 17 `name:` values exactly — a mismatch surfaces as a `StorageError` on first real read/write, not at config-load time.

### Signing in and out

Sign-in is entirely IAP's: the landing page's **Log in** button is a plain navigation to the IAP-protected URL, and IAP intercepts it to run the Google flow. It has to be a navigation, not a Streamlit rerun — a rerun replays the script over the existing websocket and would keep reading the same (missing or stale) assertion header forever.

**Sign out** (in the avatar popover) and **Sign in as a different user** (on the landing page) both go to `?gcp-iap-mode=CLEAR_LOGIN_COOKIE`, which clears IAP's login cookie and re-enters sign-in. Clearing `st.session_state` instead would achieve nothing: the next rerun would re-read the same still-valid assertion header and sign the same person straight back in.

## Keyboard shortcuts

Ctrl/Cmd+Z = undo · Ctrl/Cmd+Shift+Z or Ctrl+Y = redo (suppressed while a cell editor has focus so native text-undo still works).

## Notes & known trade-offs
- Values are edited as strings and normalized by the storage provider on publish; typed BigQuery columns need the `CAST` noted in CONFIG.md's `bigquery` section once a typed schema is in use.
- Search-match highlighting tints the whole matching cell (not just the matched substring).
- Concurrent editors aren't coordinated: last publish wins. For `gcs_parquet` specifically this is a bigger blast radius than the other providers — a full-object rewrite, not a per-cell `MERGE` — see [CONFIG.md](CONFIG.md#gcs_parquet--gcs-parquet-file--bigquery-change-log-988-gcp-deployment) for the known limitation and a possible future hardening.
- There is no row-delete feature anywhere in the app; the change-log schema supports `change_type="delete"` for completeness, but nothing produces one.
