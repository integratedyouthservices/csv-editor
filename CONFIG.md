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
  provider: mock            # ← change this: mock | gcloud_identity
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
  provider: local_csv       # ← change this: local_csv | bigquery
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
- Adding and deleting rows is supported: the whole file is rewritten on publish either way. With `id_column` set, a row added in the editor must carry a value for that column or the publish is refused (a blank key would break the next load).

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
- `audit_table`: if present, the post-publish audit write inserts one row per changed cell (`row_id, column, old_value, new_value, change_type, timestamp, user, last_updated_at, last_updated_by`). `change_type` is `insert` | `update` | `delete`; an added row logs every column with a blank `old_value`, a deleted row every column with a blank `new_value`. Create the table with those STRING columns. If the audit insert fails the publish still stands; the app shows a non-blocking warning. Omit the key entirely to disable the audit write.
- Row deletes are supported (a parameterised `DELETE` on `id_column`, run before the edits `MERGE` — the two are separate statements, so a publish that both deletes and edits is not all-or-nothing across the pair). **Adding rows is not**: the editor has no way to mint a value for `id_column`, so a publish containing a new row is refused with a clear error. Wire up whatever the real table uses (a `DEFAULT`/autoincrement column, a UUID, a sequence) in `BigQueryStorageProvider.apply_changes` to enable it.
- Current limitation: edited values are bound as STRING parameters. If your table has typed columns (FLOAT64, etc.), add per-column `CAST`s in `providers/storage/bigquery.py` (the spot is marked at the `SET` clause builder).

### Adding a new storage provider (e.g. Postgres, S3)

Same pattern as auth: subclass `StorageProvider` (`providers/storage/base.py` documents the contract — `load()` returns a DataFrame indexed by a stable unique row id; `apply_edits()` persists `{(row_id, column): value}`; `apply_changes()` adds whole-row inserts/deletes and is optional — the default refuses them rather than dropping them silently; `write_audit()` is optional), register it in `providers/storage/__init__.py`, add a settings block, point `storage.provider` at it.

---

## 4. `dataset` — columns, editors, and validation

```yaml
dataset:
  display_name: resources.csv   # shown in the toolbar and publish dialog
  identity_columns:             # composite key for change-log row matching
    - name
    - address
    - city_town
    - province_territory
  columns:                      # ORDER HERE = column order in the grid
    - name: service_category
      ...
```

Each entry in `columns:` defines one column. **`type` is the single source of truth**: it decides which editor the grid opens for that cell AND how the value is validated.

### `identity_columns` — what makes a row "the same row"

A composite natural key identifying a row across two versions of the dataset, for change-log diffing (`core/audit.py`). This dataset has no id column, so identity has to be built from real data columns.

> **Not yet wired on this branch.** `core/audit.py` and this key are present, but nothing on `master` calls them — master's publish path still writes the per-cell audit records described in the README. The consumer (a CSV import that diffs against what's published) lives on `gcp_launch`.

- Every name listed must appear in `columns:`. One that doesn't raises at config load — a typo would quietly weaken the key the audit trail is matched on.
- Choose fields that *identify* a service, not ones that *describe* it. Editing an identity column makes that row read as a `delete` plus an `insert` rather than an `update`, because the renamed row no longer matches.
- Uniqueness is not enforced. Rows sharing a key are paired off in file order within that group; surplus on either side becomes an insert or a delete.
- Omit the key (or leave it empty) to fall back to logging every row as an `insert`.

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
