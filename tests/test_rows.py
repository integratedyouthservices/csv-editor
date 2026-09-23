"""Adding and deleting whole rows: the grid, undo, review, publish.

Run with:  python -m pytest tests/ -q

These drive the whole app, so they need a dataset that is actually
present. Rather than lean on whatever config.yaml points at (the 988
parquet object isn't in the repo), the module fixture below stands up a
throwaway local_csv dataset with the shipped column spec and points the
app at it -- same zero-setup principle as the rest of the suite.
"""
import csv
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CSV_EDITOR_TESTMODE", "1")

import pytest
import streamlit as st
import yaml
from streamlit.testing.v1 import AppTest

from providers.auth.base import User
from providers.storage import create_storage_provider

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")

CSV_PATH = None       # set by the fixture; the dataset under test
DATA_COLUMNS = []     # its column names, in order


@pytest.fixture(scope="module", autouse=True)
def dataset():
    """A temp CSV dataset + config for this module, with the app's
    cached config/provider reset either side so the swap actually takes
    (both are @st.cache_resource, which outlives an AppTest session)."""
    global CSV_PATH
    tmp = tempfile.mkdtemp()
    CSV_PATH = os.path.join(tmp, "resources.csv")
    shutil.copy(os.path.join(ROOT, "sample_data", "resources.csv"), CSV_PATH)

    raw = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    raw["auth"]["provider"] = "mock"
    raw["storage"]["provider"] = "local_csv"
    raw["storage"]["local_csv"] = {"path": CSV_PATH, "id_column": None}
    raw["dataset"]["display_name"] = "resources.csv"
    cfg_path = os.path.join(tmp, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, allow_unicode=True, sort_keys=False)

    # the column spec has to match the file, or every cell fails validation
    header = next(csv.reader(open(CSV_PATH, encoding="utf-8")))
    assert [c["name"] for c in raw["dataset"]["columns"]] == header
    DATA_COLUMNS[:] = header

    previous = os.environ.get("CSV_EDITOR_CONFIG")
    os.environ["CSV_EDITOR_CONFIG"] = cfg_path
    st.cache_resource.clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CSV_EDITOR_CONFIG", None)
        else:
            os.environ["CSV_EDITOR_CONFIG"] = previous
        st.cache_resource.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def _user():
    return User(username="admin", display_name="admin", provider="mock")


def fresh(**session):
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.session_state["user"] = session.pop("user", _user())
    at = at.run()
    for k, v in session.items():
        at.session_state[k] = v
    return at.run()


def button(at, text):
    return [b for b in at.button if text in (b.label or "")][0]


_nonce = iter(range(1, 10_000))


def bridge(at, payload):
    """Mimic the grid script posting through the hidden bridge input —
    nonce included, since that is what makes two identical actions in a
    row two separate on_change events."""
    at.text_input(key="cell_bridge").set_value(
        json.dumps({**payload, "n": next(_nonce)})
    )
    return at.run()


# ------------------------------------------------------------ adding


def test_add_row_appends_blank_row_and_focuses_it():
    at = fresh()
    before = len(at.session_state["original_df"])
    at = button(at, "Add Row").click().run()
    assert not at.exception

    added = at.session_state["added_rows"]
    assert len(added) == 1
    df = at.session_state["original_df"]
    assert len(df) == before + 1
    assert df.index[-1] == added[0]
    assert all(v == "" for v in df.loc[added[0]])
    # the grid is told which row to open an editor on
    assert at.session_state["focus_token"] == 1
    grid = " ".join(md.value for md in at.markdown)
    assert 'data-focus-pos="%d"' % (len(df) - 1) in grid
    assert "de-row-added" in grid


def test_add_row_twice_reuses_the_blank_row_at_the_bottom():
    at = fresh()
    at = button(at, "Add Row").click().run()
    at = button(at, "Add Row").click().run()
    assert len(at.session_state["added_rows"]) == 1      # no second blank row
    assert at.session_state["focus_token"] == 2          # but focus moved there again


def test_blank_added_row_is_not_a_pending_change():
    at = fresh()
    at = button(at, "Add Row").click().run()
    labels = " ".join(b.label or "" for b in at.button)
    assert "Review changes (" not in labels      # nothing to review yet
    assert button(at, "Review changes").disabled

    # type into the row → it becomes a change worth reviewing
    rid = at.session_state["added_rows"][0]
    pos = at.session_state["_row_ids_editing"].index(rid)
    at = bridge(at, {"page": "editing", "pos": pos, "col": "name", "value": "New Place"})
    assert at.session_state["edits"][(rid, "name")] == "New Place"
    assert "Review changes (1)" in " ".join(b.label or "" for b in at.button)


def test_undo_removes_an_added_row_and_redo_brings_it_back():
    at = fresh()
    at = button(at, "Add Row").click().run()
    rid = at.session_state["added_rows"][0]
    pos = at.session_state["_row_ids_editing"].index(rid)
    at = bridge(at, {"page": "editing", "pos": pos, "col": "name", "value": "New Place"})

    at = button(at, "Undo").click().run()            # undo the cell edit
    assert at.session_state["added_rows"] == [rid]
    at = button(at, "Undo").click().run()            # undo the add itself
    assert at.session_state["added_rows"] == []
    assert rid not in at.session_state["original_df"].index
    assert at.session_state["edits"] == {}

    at = button(at, "Redo").click().run()
    assert at.session_state["added_rows"] == [rid]
    at = button(at, "Redo").click().run()
    assert at.session_state["edits"][(rid, "name")] == "New Place"


def test_added_row_stays_visible_while_searching():
    at = fresh(search="toronto")
    at = button(at, "Add Row").click().run()
    rid = at.session_state["added_rows"][0]
    assert rid in at.session_state["_row_ids_editing"]


def test_review_shows_every_cell_of_an_added_row_as_edited():
    at = fresh()
    at = button(at, "Add Row").click().run()
    rid = at.session_state["added_rows"][0]
    pos = at.session_state["_row_ids_editing"].index(rid)
    at = bridge(at, {"page": "editing", "pos": pos, "col": "name", "value": "New Place"})
    at = button(at, "Review changes").click().run()
    assert not at.exception

    body = " ".join(md.value for md in at.markdown)
    assert "1 row added" in body
    assert "New row" in body
    # every data cell of the new row is marked as changed — the edited
    # tint, or amber/red where the new row's value doesn't validate
    # (exactly what a cell edited by hand would show)
    row = body[body.index("de-row-added"):]
    row = row[: row.index("</tr>")]
    n_columns = len(at.session_state["original_df"].columns)
    marked = sum(row.count(c) for c in ("de-cell-edit", "de-cell-warn", "de-cell-err"))
    assert marked == n_columns
    assert row.count("de-cell-warn") >= 1        # required columns still blank


# ---------------------------------------------------------- deleting


def test_right_click_delete_arms_the_modal_and_ok_deletes():
    at = fresh()
    rid = at.session_state["_row_ids_editing"][3]
    at = bridge(at, {"page": "editing", "pos": 3, "action": "delete_row"})
    assert at.session_state["pending_delete"] == rid
    assert at.session_state["deleted_rows"] == []       # nothing yet — just the modal

    at = button(at, "Cancel").click().run()
    assert at.session_state["pending_delete"] is None
    assert at.session_state["deleted_rows"] == []

    at = bridge(at, {"page": "editing", "pos": 3, "action": "delete_row"})
    at = [b for b in at.button if (b.label or "") == "OK"][0].click().run()
    assert at.session_state["deleted_rows"] == [rid]
    assert rid not in at.session_state["_row_ids_editing"]   # gone from the editor
    assert "Review changes (1)" in " ".join(b.label or "" for b in at.button)


def test_undo_restores_a_deleted_row():
    at = fresh()
    rid = at.session_state["_row_ids_editing"][3]
    at = bridge(at, {"page": "editing", "pos": 3, "action": "delete_row"})
    at = [b for b in at.button if (b.label or "") == "OK"][0].click().run()

    at = button(at, "Undo").click().run()
    assert at.session_state["deleted_rows"] == []
    assert rid in at.session_state["_row_ids_editing"]
    at = button(at, "Redo").click().run()
    assert at.session_state["deleted_rows"] == [rid]


def test_deleting_an_added_row_just_takes_it_back_out():
    at = fresh()
    at = button(at, "Add Row").click().run()
    rid = at.session_state["added_rows"][0]
    pos = at.session_state["_row_ids_editing"].index(rid)
    at = bridge(at, {"page": "editing", "pos": pos, "action": "delete_row"})
    at = [b for b in at.button if (b.label or "") == "OK"][0].click().run()
    assert at.session_state["added_rows"] == []
    assert at.session_state["deleted_rows"] == []
    assert rid not in at.session_state["original_df"].index


def test_review_shows_a_deleted_row_as_dead():
    at = fresh()
    rid = at.session_state["_row_ids_editing"][3]
    at = bridge(at, {"page": "editing", "pos": 3, "action": "delete_row"})
    at = [b for b in at.button if (b.label or "") == "OK"][0].click().run()
    at = button(at, "Review changes").click().run()
    assert not at.exception

    body = " ".join(md.value for md in at.markdown)
    assert "1 row deleted" in body and "Row deleted" in body
    row = body[body.index("de-row-deleted"):]
    row = row[: row.index("</tr>")]
    assert 'data-editable="1"' not in row       # no editing a row on its way out
    assert at.session_state["_row_ids_review"] == [rid]


def test_deleted_row_with_invalid_edit_does_not_block_publish():
    at = fresh(edits={(3, "website"): "not-a-url"}, view="review")
    assert button(at, "Publish").disabled
    at.session_state["deleted_rows"] = [3]
    at = at.run()
    assert not button(at, "Publish").disabled


# ----------------------------------------------------------- publish


def _temp_dataset():
    tmp = tempfile.mkdtemp()
    shutil.copy(os.path.join(ROOT, "sample_data", "resources.csv"),
                os.path.join(tmp, "resources.csv"))
    return tmp


def test_local_csv_applies_inserts_and_deletes():
    tmp = _temp_dataset()
    try:
        store = create_storage_provider("local_csv", {"path": os.path.join(tmp, "resources.csv")})
        df = store.load()
        before = len(df)
        new_row = {c: "" for c in df.columns}
        new_row["name"] = "Brand New Service"
        new_row["city_town"] = "Ottawa"

        store.apply_changes(df, {(df.index[0], "city_town"): "Toronto"},
                            inserts=[new_row], deletes=[df.index[1]])

        after = store.load()
        assert len(after) == before          # one in, one out
        assert after.iloc[0]["city_town"] == "Toronto"
        assert after.iloc[-1]["name"] == "Brand New Service"
        assert df.iloc[1]["name"] not in list(after["name"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_provider_without_row_support_refuses_rather_than_dropping():
    from providers.storage.base import StorageError, StorageProvider

    class Minimal(StorageProvider):
        name = "minimal"
        applied = None

        def load(self):
            raise NotImplementedError

        def apply_edits(self, df, edits):
            type(self).applied = edits

    store = Minimal({})
    store.apply_changes(None, {("a", "b"): "c"})
    assert Minimal.applied == {("a", "b"): "c"}

    try:
        store.apply_changes(None, {}, inserts=[{"a": "b"}])
    except StorageError as exc:
        assert "cannot add or delete rows" in str(exc)
    else:
        raise AssertionError("expected a StorageError")


def test_publish_writes_new_row_and_removes_deleted_row_with_change_log():
    at = fresh()
    doomed = at.session_state["_row_ids_editing"][2]
    doomed_name = at.session_state["original_df"].at[doomed, "name"]
    before = len(at.session_state["original_df"])

    at = button(at, "Add Row").click().run()
    rid = at.session_state["added_rows"][0]
    pos = at.session_state["_row_ids_editing"].index(rid)
    at = bridge(at, {"page": "editing", "pos": pos, "col": "name", "value": "Brand New"})

    at = bridge(at, {"page": "editing", "pos": 2, "action": "delete_row"})
    at = [b for b in at.button if (b.label or "") == "OK"][0].click().run()

    at = button(at, "Review changes").click().run()
    at = button(at, "Publish").click().run()
    # AppTest reruns the whole script rather than the dialog fragment, so
    # the confirm button only exists on a run where the dialog is
    # (re)opened — press both in the same run.
    button(at, "Publish changes").set_value(True)
    button(at, "Yes, publish").set_value(True)
    at = at.run()
    assert not at.exception
    assert not at.error

    # one row in, one row out, and the frame was reloaded from the file
    names = list(at.session_state["original_df"]["name"])
    assert "Brand New" in names
    assert doomed_name not in names
    assert len(at.session_state["original_df"]) == before
    assert at.session_state["added_rows"] == []
    assert at.session_state["deleted_rows"] == []

    entry = json.loads(
        open(CSV_PATH + ".audit.jsonl", encoding="utf-8").read().splitlines()[-1]
    )
    records = entry["records"]
    inserted = [r for r in records if r["change_type"] == "insert"]
    deleted = [r for r in records if r["change_type"] == "delete"]
    assert len(inserted) == 1 and len(deleted) == 1

    # an added row is one "after" row carrying the new values, with
    # nothing before it; a deleted row is the mirror image
    assert inserted[0]["change_state"] == "after"
    assert inserted[0]["name"] == "Brand New"
    assert deleted[0]["change_state"] == "before"
    assert deleted[0]["name"] == doomed_name
    assert not [r for r in records if r["change_state"] == "before"
                and r["change_type"] == "insert"]

    # every row carries the full column set plus the change-log metadata
    for record in records:
        assert set(record) == set(DATA_COLUMNS) | {
            "change_id", "change_state", "change_type", "changed_by", "changed_at",
        }
        assert record["changed_by"] == "admin"
    assert len({r["change_id"] for r in records}) == len(records)   # unrelated changes


def test_a_row_of_blank_spaces_is_not_a_change_to_publish():
    at = fresh()
    at = button(at, "Add Row").click().run()
    rid = at.session_state["added_rows"][0]
    pos = at.session_state["_row_ids_editing"].index(rid)
    at = bridge(at, {"page": "editing", "pos": pos, "col": "name", "value": "   "})

    assert at.session_state["edits"] == {}       # whitespace trimmed on commit
    assert at.session_state["added_rows"] == [rid]   # the row is still on screen
    # ...but there is nothing to review, so nothing can be published
    assert button(at, "Review changes").disabled
