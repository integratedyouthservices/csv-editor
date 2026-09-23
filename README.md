# CSV Data Editor (Streamlit)

Streamlit implementation of the IYS "Data Editor" wireframes: authenticated users edit cells of a tabular dataset in a grid, filter rows with live search, review pending changes as old → new diffs with per-cell validation, and publish behind a confirmation dialog.

Auth and storage are **pluggable providers selected in `config.yaml`** (see **[CONFIG.md](CONFIG.md)** for the full guide to editing authentication, storage, and column/validation settings) — swap Google Cloud auth or BigQuery for something else without touching application code.

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py
```

Log in with `admin` / `admin` (the dev `mock` auth provider — see `config.yaml`). The default storage provider is `local_csv` backed by `sample_data/resources.csv` (96 sample crisis-line / community-resource rows, including required-blank cells to exercise the amber warnings), so the app runs end-to-end with no cloud setup.

## Screens → wireframes

| Wireframe | Where |
|---|---|
| 1a Login | centered card, logo mark, email/password, "Forgot password?" |
| 1b Editing | toolbar (title, file info, search, "Review changes (n)" badge, avatar), grid, footer legend + `rows x–y of n` pagination |
| 2a Search | as-you-type filtering, blue filter bar (`Showing x of n rows matching "…"`), Clear search, yellow match highlighting; **edits on hidden rows are kept and the badge count is unchanged** |
| 1c Review | only edited rows, green-tinted changed cells showing `old → new`, cells still editable, summary "n rows · m cells changed", plus a diff panel with struck-through old values |
| 1d Validation | invalid cells get the orange fill + red outline + inline `✗ …` message; summary turns red ("k cells invalid"); Publish disabled (grey, dashed border) |
| 1e Publish | modal dialog with dynamic cell/row counts, "download a backup copy first", Cancel / "Yes, publish" |
| Add / delete rows | **＋ Add Row** at the right of the undo/redo strip appends a blank row and opens its first cell; right-click a row for **✕ Delete Row**, confirmed by a Cancel/OK modal |

### One deliberate adaptation
Streamlit's `st.data_editor` cannot style individual editable cells, but the wireframes lean heavily on per-cell visual states (edited tint, invalid outline, match highlight). So the grid is a **styled `st.dataframe` with single-cell selection**: clicking a cell opens an inline editor directly beneath the grid (Enter commits; "Revert to original" clears the edited state, per spec). This preserves every visual state at the cost of a one-click hop instead of type-in-place. If type-in-place matters more than the visual states, swap the grid for `st.data_editor` — the edit-tracking model (`edits` keyed by `(row_id, column)`) supports either front end.

## Architecture

```
app.py                      Streamlit UI (login / editing / review / publish)
config.yaml                 provider selection + column & validation spec
core/
  config.py                 config loader, ColumnRule dataclass
  validation.py             type / length / regex validation engine
providers/
  auth/
    base.py                 AuthProvider ABC + User
    mock.py                 dev users from config
    gcloud_identity.py      Google Cloud Identity Platform (email+password REST)
    __init__.py             registry + factory (lazy imports)
  storage/
    base.py                 StorageProvider ABC (load / apply_edits contract)
    local_csv.py            local file, atomic writes
    bigquery.py             BigQuery, atomic MERGE publish
    __init__.py             registry + factory (lazy imports)
tests/test_app.py           end-to-end view tests (Streamlit AppTest)
```

### State model (matches the handoff spec)
- `user` — session auth
- `original_df` — dataset from the storage provider, indexed by a stable `_row_id`
- `edits` — `{(row_id, column): new_value}`; reverting a cell to its original value removes the entry
- `added_rows` / `deleted_rows` — pending whole-row changes (see below)
- `search` → derived filtered row list (search runs over data **with edits applied**)
- `validationErrors` — derived from `edits` + column rules on every rerun
- `view` — `editing | review`; publish and delete-row dialogs via `st.dialog`
- Publish → provider `apply_changes()` → merge into `original_df`, clear `edits`

## Swapping providers (requirements A & B)

Everything is driven by two lines in `config.yaml`:

```yaml
auth:
  provider: gcloud_identity   # or: mock
storage:
  provider: bigquery          # or: local_csv
```

### Auth: Google Cloud (current)
`gcloud_identity` calls the Identity Platform `signInWithPassword` REST endpoint. Set the API key via env var:

```bash
export GCP_IDENTITY_API_KEY="AIza..."
```

Bad credentials return a normal "Incorrect email or password" message; infrastructure problems (missing key, network) surface separately as `AuthError`.

### Storage: BigQuery (current)
```yaml
storage:
  provider: bigquery
  bigquery:
    project: my-gcp-project
    dataset: my_dataset
    table: customers
    id_column: id        # REQUIRED: stable unique key
```

```bash
pip install google-cloud-bigquery db-dtypes
gcloud auth application-default login   # or GOOGLE_APPLICATION_CREDENTIALS
```

Publishing runs a **single parameterized `MERGE`**, so all cells land atomically or not at all. Note: edited values are bound as STRING parameters; when the real column spec arrives, add per-column `CAST`s in `bigquery.py` for non-string BigQuery column types (the spot is marked by the `SET` clause builder).

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

The contract is documented in `providers/storage/base.py`: `load()` must return a DataFrame indexed by a stable unique row id; `apply_edits()` receives already-validated `{(row_id, column): value}` and should persist atomically where the backend allows. Same pattern for auth in `providers/auth/`.

## Columns & validation

The dataset is the 17-column crisis-line / community-resource schema from the design handoff. Everything is config (`dataset.columns` in `config.yaml`); the `type` field is the single source of truth for BOTH the cell editor the grid opens and validation:

```yaml
- name: service_category
  type: enum          # dropdown editor; must be one of `options`
  required: true
  options: ["211 and Other Resource Databases", ...]   # 9 values
- name: postal_code
  type: text
  normalize: postal_code       # trim + "k1a0b1" → "K1A 0B1"
  regex: "^[A-Za-z][0-9][A-Za-z] [0-9][A-Za-z][0-9]$"
  regex_hint: "must match A1A 1A1"
- name: latitude
  type: float          # number editor with min/max
  required: true
  min: -90
  max: 90
- name: description
  type: textarea       # long text (grid opens a single-line editor —
                       # Streamlit has no multi-line cell editor)
```

**Two validation severities**, per the spec: required-but-blank cells are flagged inline (amber ⚠ "required — currently blank") but do **not** block publish; format/type failures (enum, float range, regex) show red ✗ errors and **do** block publish. `phone_1` is required but has no format rule (the data has mixed formats). All text is whitespace-trimmed on commit. Header glyphs mark types: `*` required · `▾` dropdown · `¶` text area · `#.#` float.

## Audit / metadata (second request)

After a successful publish the app issues a second write via `StorageProvider.write_audit(metadata, records)`:
- `metadata` = `{last_updated_at, last_updated_by}` (the signed-in email)
- `records` = one entry per changed cell `{row_id, column, old_value, new_value, change_type, timestamp, user}`, where `change_type` is `insert` | `update` | `delete` — an added row logs **every** column with a blank `old_value`, a deleted row every column with a blank `new_value`

If the audit write fails the CSV publish still stands and the app shows a non-blocking warning. `local_csv` appends JSON lines to `<csv>.audit.jsonl`; `bigquery` inserts into `storage.bigquery.audit_table` (omit that key to skip). The publish dialog notes "saved as <email> · a second request records who changed what and when".

### `core/audit.py` — before/after change-log rows (present, not yet wired)

`core/audit.py` builds a richer, row-level change log than the per-cell records above: each change appends the dataset's full column set plus `change_id`, `change_state` (`before`/`after`), `change_type` (`insert`/`update`/`delete`), `changed_by`, `changed_at` — an update writing a before/after pair under one `change_id`.

It also provides `rows_for_replace_diff`, which diffs an uploaded file against what's published on `dataset.identity_columns` (see [CONFIG.md](CONFIG.md)) so a full-file replace logs only what actually moved, rather than one `insert` per row.

**Nothing on `master` calls any of it yet** — the publish path here still writes the per-cell records described above, and there is no CSV import feature on this branch. The module and its tests (`tests/test_audit.py`, 31 cases) are in place so the behaviour is pinned; the wiring lives on `gcp_launch` along with the `gcs_parquet` provider and the BigQuery `change_log` table it writes to.

## Adding & deleting rows

**＋ Add Row** sits at the right of the undo/redo strip and appends a blank row at the bottom of the table, opening an editor on its first cell. If the bottom row is already blank it just puts the cursor back in that one, so empty rows never stack up — and a row that is nothing but whitespace is dropped at publish rather than saved.

**Delete Row** comes from right-clicking anywhere on a row outside a cell editor (inside one, the browser's own menu is the useful one). The menu item opens a Cancel / OK confirmation modal; OK marks the row, which then disappears from the editing grid.

Both are ordinary pending changes until publish:

| | on the editing grid | on Review changes | in the audit log |
|---|---|---|---|
| added row | green row marker, cells editable | every cell reads as changed — edited tint, or the usual amber/red where the new value doesn't validate | one record per column, `old_value` blank, `change_type: insert` |
| deleted row | hidden | greyed, struck through, not editable | one record per column, `new_value` blank, `change_type: delete` |

Undo/redo covers both (undo after Add Row removes the row and its typed values; undo after a delete brings the row back), as does ↩ on the review screen's change list. A new row is validated whole, so its blank required columns raise the usual amber warnings; a deleted row is skipped by validation entirely, so an invalid value on a row you're removing doesn't block the publish.

Persistence goes through `StorageProvider.apply_changes(df, edits, inserts, deletes)`. `local_csv` supports both (it rewrites the whole file anyway). `bigquery` supports deletes; adding rows is refused with a clear error because nothing can mint the table's `id_column` value yet — see [CONFIG.md](CONFIG.md). A provider that doesn't override `apply_changes` refuses inserts/deletes rather than publishing a partial change set.

## Keyboard shortcuts

Ctrl/Cmd+Z = undo · Ctrl/Cmd+Shift+Z or Ctrl+Y = redo (suppressed while a cell editor has focus so native text-undo still works).

## Tests

```bash
CSV_EDITOR_TESTMODE=1 python -m pytest tests/ -q
```

Covers: login success/failure, review summary + publish gating on validation errors, search filtering keeping hidden-row edits and the badge count, the editing view render, and (`tests/test_rows.py`) adding/deleting rows end to end — undo/redo, the confirm modal, how both look on Review changes, the publish write and its audit records. (`CSV_EDITOR_TESTMODE` disables dataframe cell-selection, which Streamlit's test harness can't serialize; it has no other effect.)

## Notes & known trade-offs
- Values are edited as strings and normalized by the storage provider on publish; typed BigQuery columns need the `CAST` noted above once the real schema lands.
- Search-match highlighting tints the whole matching cell (Streamlit can't style substrings inside grid cells).
- Concurrent editors aren't coordinated: last publish wins per cell. Add optimistic locking in a provider if needed.
- The grid's browser-side layer (double-click editor, right-click row menu, new-row focus) has no automated coverage — the Python tests drive it through the same bridge payloads the script sends, which pins the behaviour either side of the JS but not the JS itself.
- The "Forgot password?" link is a placeholder — wire it to the auth provider's reset flow when the real IdP is connected.
