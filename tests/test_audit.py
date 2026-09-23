"""Change-log row construction: before/after pairing and the import diff.

The change log is the compliance record for this dataset, so what matters
here is that a change produces exactly the rows a later query expects -- one
change_id per logical change, "before" carrying the values that were
replaced, "after" carrying the ones that landed.

Run with:  python -m pytest tests/ -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from core.audit import (
    CHANGE_STATE_AFTER,
    CHANGE_STATE_BEFORE,
    CHANGE_TYPE_DELETE,
    CHANGE_TYPE_INSERT,
    CHANGE_TYPE_UPDATE,
    delete_row,
    insert_row,
    new_change_id,
    rows_for_edits,
    rows_for_full_replace,
    rows_for_replace_diff,
    update_rows,
)

COLUMNS = ["name", "city", "phone", "hours"]
IDENTITY = ["name", "city"]
WHO = "editor@example.org"
WHEN = "2026-09-23T12:00:00+00:00"


def frame(*rows: dict) -> pd.DataFrame:
    df = pd.DataFrame(list(rows), columns=COLUMNS)
    df.index = pd.RangeIndex(len(df), name="_row_id")
    return df


def row(name, city, phone="555-0100", hours="9-5") -> dict:
    return {"name": name, "city": city, "phone": phone, "hours": hours}


def of_type(records, change_type):
    return [r for r in records if r["change_type"] == change_type]


def paired(records):
    """Group records by change_id, preserving first-seen order."""
    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(r["change_id"], []).append(r)
    return groups


# --- id format ------------------------------------------------------------


def test_change_id_has_the_documented_shape():
    cid = new_change_id()
    assert cid.startswith("CHG-")
    assert len(cid) == 12
    assert cid[4:].isupper()


def test_change_ids_are_unique_per_change():
    assert len({new_change_id() for _ in range(500)}) == 500


# --- the three change types ----------------------------------------------


def test_update_emits_two_rows_sharing_one_change_id():
    before, after = row("A", "Ottawa"), row("A", "Ottawa", phone="555-0199")
    records = update_rows(before, after, COLUMNS, WHO, WHEN)

    assert len(records) == 2
    assert records[0]["change_id"] == records[1]["change_id"]
    assert records[0]["change_state"] == CHANGE_STATE_BEFORE
    assert records[1]["change_state"] == CHANGE_STATE_AFTER
    assert records[0]["phone"] == "555-0100"
    assert records[1]["phone"] == "555-0199"
    assert all(r["change_type"] == CHANGE_TYPE_UPDATE for r in records)
    assert all(r["changed_by"] == WHO and r["changed_at"] == WHEN for r in records)


def test_insert_emits_one_after_row_only():
    record = insert_row(row("A", "Ottawa"), COLUMNS, WHO, WHEN)
    assert record["change_state"] == CHANGE_STATE_AFTER
    assert record["change_type"] == CHANGE_TYPE_INSERT


def test_delete_emits_one_before_row_only():
    record = delete_row(row("A", "Ottawa"), COLUMNS, WHO, WHEN)
    assert record["change_state"] == CHANGE_STATE_BEFORE
    assert record["change_type"] == CHANGE_TYPE_DELETE


def test_every_row_carries_all_data_columns_plus_metadata():
    record = insert_row(row("A", "Ottawa"), COLUMNS, WHO, WHEN)
    assert set(record) == set(COLUMNS) | {
        "change_id",
        "change_state",
        "change_type",
        "changed_by",
        "changed_at",
    }


def test_none_is_written_as_empty_string_not_the_string_none():
    record = insert_row({"name": "A", "city": None}, COLUMNS, WHO, WHEN)
    assert record["city"] == ""
    assert record["phone"] == ""


# --- cell edits -----------------------------------------------------------


def test_multiple_edited_cells_in_one_row_make_a_single_pair():
    df = frame(row("A", "Ottawa"), row("B", "Hull"))
    edits = {(0, "phone"): "555-0199", (0, "hours"): "24/7"}

    records = rows_for_edits(df, edits, COLUMNS, WHO, WHEN)

    assert len(records) == 2
    before, after = records
    assert before["phone"] == "555-0100" and before["hours"] == "9-5"
    assert after["phone"] == "555-0199" and after["hours"] == "24/7"


def test_edits_to_two_rows_make_two_separate_pairs():
    df = frame(row("A", "Ottawa"), row("B", "Hull"))
    edits = {(0, "phone"): "555-0199", (1, "phone"): "555-0200"}

    groups = paired(rows_for_edits(df, edits, COLUMNS, WHO, WHEN))

    assert len(groups) == 2
    assert all(len(g) == 2 for g in groups.values())


def test_the_before_row_carries_unedited_columns_too():
    df = frame(row("A", "Ottawa"))
    records = rows_for_edits(df, {(0, "phone"): "555-0199"}, COLUMNS, WHO, WHEN)
    assert records[0]["name"] == "A" and records[0]["hours"] == "9-5"


def test_no_edits_logs_nothing():
    assert rows_for_edits(frame(row("A", "Ottawa")), {}, COLUMNS, WHO, WHEN) == []


# --- import diff ----------------------------------------------------------


def test_unchanged_rows_log_nothing():
    df = frame(row("A", "Ottawa"), row("B", "Hull"))
    assert rows_for_replace_diff(df, df.copy(), COLUMNS, IDENTITY, WHO, WHEN) == []


def test_changed_row_logs_one_update_pair():
    old = frame(row("A", "Ottawa"), row("B", "Hull"))
    new = frame(row("A", "Ottawa", phone="555-0199"), row("B", "Hull"))

    records = rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN)

    assert len(records) == 2
    before, after = records
    assert before["change_id"] == after["change_id"]
    assert before["phone"] == "555-0100"
    assert after["phone"] == "555-0199"
    assert before["change_type"] == CHANGE_TYPE_UPDATE


def test_new_key_logs_an_insert():
    old = frame(row("A", "Ottawa"))
    new = frame(row("A", "Ottawa"), row("B", "Hull"))

    inserts = of_type(
        rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN), CHANGE_TYPE_INSERT
    )

    assert len(inserts) == 1
    assert inserts[0]["name"] == "B"
    assert inserts[0]["change_state"] == CHANGE_STATE_AFTER


def test_dropped_key_logs_a_delete_capturing_what_was_removed():
    old = frame(row("A", "Ottawa"), row("B", "Hull"))
    new = frame(row("A", "Ottawa"))

    deletes = of_type(
        rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN), CHANGE_TYPE_DELETE
    )

    assert len(deletes) == 1
    assert deletes[0]["name"] == "B"
    assert deletes[0]["phone"] == "555-0100"
    assert deletes[0]["change_state"] == CHANGE_STATE_BEFORE


def test_reordering_the_file_is_not_a_change():
    old = frame(row("A", "Ottawa"), row("B", "Hull"), row("C", "Laval"))
    new = frame(row("C", "Laval"), row("A", "Ottawa"), row("B", "Hull"))

    assert rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN) == []


def test_inserting_a_row_at_the_top_does_not_shift_every_other_row():
    """The positional-index failure mode the identity key exists to avoid."""
    old = frame(row("A", "Ottawa"), row("B", "Hull"))
    new = frame(row("Z", "Laval"), row("A", "Ottawa"), row("B", "Hull"))

    records = rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN)

    assert len(of_type(records, CHANGE_TYPE_UPDATE)) == 0
    assert len(of_type(records, CHANGE_TYPE_INSERT)) == 1


def test_editing_an_identity_column_reads_as_a_delete_plus_an_insert():
    """Documented trade-off of a natural key: renaming breaks the match."""
    old = frame(row("A", "Ottawa"))
    new = frame(row("A renamed", "Ottawa"))

    records = rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN)

    assert len(of_type(records, CHANGE_TYPE_INSERT)) == 1
    assert len(of_type(records, CHANGE_TYPE_DELETE)) == 1
    assert of_type(records, CHANGE_TYPE_UPDATE) == []


def test_same_name_in_different_cities_are_different_rows():
    old = frame(row("Crisis Line", "Ottawa"), row("Crisis Line", "Hull"))
    new = frame(
        row("Crisis Line", "Ottawa"), row("Crisis Line", "Hull", phone="555-0199")
    )

    records = rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN)

    assert len(records) == 2
    assert records[0]["city"] == "Hull"


def test_a_mixed_import_logs_each_change_once():
    old = frame(row("A", "Ottawa"), row("B", "Hull"), row("C", "Laval"))
    new = frame(
        row("A", "Ottawa"),                      # unchanged
        row("B", "Hull", hours="24/7"),          # updated
        row("D", "Gatineau"),                    # inserted ("C" dropped)
    )

    records = rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN)

    assert len(of_type(records, CHANGE_TYPE_UPDATE)) == 2  # one before/after pair
    assert len(of_type(records, CHANGE_TYPE_INSERT)) == 1
    assert len(of_type(records, CHANGE_TYPE_DELETE)) == 1
    assert len(paired(records)) == 3


# --- duplicate identity keys ---------------------------------------------


def test_duplicate_keys_pair_off_in_order_rather_than_failing():
    old = frame(row("A", "Ottawa", phone="1"), row("A", "Ottawa", phone="2"))
    new = frame(row("A", "Ottawa", phone="1"), row("A", "Ottawa", phone="9"))

    records = rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN)

    assert len(records) == 2
    assert records[0]["phone"] == "2"
    assert records[1]["phone"] == "9"


def test_extra_duplicate_in_the_upload_is_an_insert():
    old = frame(row("A", "Ottawa"))
    new = frame(row("A", "Ottawa"), row("A", "Ottawa"))

    records = rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN)

    assert len(of_type(records, CHANGE_TYPE_INSERT)) == 1
    assert of_type(records, CHANGE_TYPE_UPDATE) == []


def test_fewer_duplicates_in_the_upload_is_a_delete():
    old = frame(row("A", "Ottawa"), row("A", "Ottawa"))
    new = frame(row("A", "Ottawa"))

    records = rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN)

    assert len(of_type(records, CHANGE_TYPE_DELETE)) == 1
    assert of_type(records, CHANGE_TYPE_UPDATE) == []


# --- fallbacks ------------------------------------------------------------


def test_without_identity_columns_every_row_logs_as_an_insert():
    old = frame(row("A", "Ottawa"))
    new = frame(row("A", "Ottawa"), row("B", "Hull"))

    records = rows_for_replace_diff(old, new, COLUMNS, [], WHO, WHEN)

    assert len(of_type(records, CHANGE_TYPE_INSERT)) == 2
    assert len(records) == len(rows_for_full_replace(new, COLUMNS, WHO, WHEN))


def test_importing_into_an_empty_dataset_is_all_inserts():
    old = frame()
    new = frame(row("A", "Ottawa"), row("B", "Hull"))

    records = rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN)

    assert len(of_type(records, CHANGE_TYPE_INSERT)) == 2


def test_importing_an_empty_file_deletes_everything():
    old = frame(row("A", "Ottawa"), row("B", "Hull"))
    new = frame()

    records = rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN)

    assert len(of_type(records, CHANGE_TYPE_DELETE)) == 2


def test_values_are_compared_as_strings_so_parquet_floats_do_not_look_changed():
    old = pd.DataFrame([{"name": "A", "city": "Ottawa", "phone": "555", "hours": 9.0}])
    new = pd.DataFrame([{"name": "A", "city": "Ottawa", "phone": "555", "hours": "9.0"}])

    assert rows_for_replace_diff(old, new, COLUMNS, IDENTITY, WHO, WHEN) == []


# --- identity_columns config ---------------------------------------------


def config_with(identity):
    from core.config import AppConfig

    return AppConfig(
        raw={
            "dataset": {
                "columns": [{"name": c} for c in COLUMNS],
                "identity_columns": identity,
            }
        }
    )


def test_identity_columns_are_read_from_the_dataset_block():
    assert config_with(["name", "city"]).identity_columns == ["name", "city"]


def test_identity_columns_default_to_empty_when_absent():
    from core.config import AppConfig

    cfg = AppConfig(raw={"dataset": {"columns": [{"name": c} for c in COLUMNS]}})
    assert cfg.identity_columns == []


def test_an_unknown_identity_column_is_a_loud_error_not_a_silent_drop():
    with pytest.raises(ValueError, match="citty"):
        _ = config_with(["name", "citty"]).identity_columns


def test_the_shipped_config_identity_columns_all_exist():
    """config.yaml's key must resolve against its own 17 columns."""
    from core.config import load_config

    cfg = load_config(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
    )
    assert cfg.identity_columns  # configured, not empty
    assert set(cfg.identity_columns) <= {c.name for c in cfg.columns}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
