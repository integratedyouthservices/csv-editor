"""Unit tests for the columnRules validation engine and the audit write.

Run with:  python -m pytest tests/ -q
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import load_config
from core.validation import (
    ERROR,
    WARNING,
    errors_only,
    normalize_value,
    validate_cell,
    validate_edits,
    warnings_only,
)
from providers.storage import create_storage_provider

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = {c.name: c for c in load_config(os.path.join(ROOT, "config.yaml")).columns}


def test_schema_has_17_columns():
    assert len(RULES) == 17
    assert RULES["service_category"].type == "enum"
    assert len(RULES["service_category"].options) == 9
    # both spellings are valid: 15 codes (incl. CA/DC for international
    # lines) + 13 full province/territory names
    assert len(RULES["province_territory"].options) == 28
    assert "ON" in RULES["province_territory"].options
    assert "Ontario" in RULES["province_territory"].options
    assert RULES["description"].type == "textarea"
    assert RULES["latitude"].type == "float"


def test_postal_code_normalize_and_validate():
    assert normalize_value("  k1a0b1 ", RULES["postal_code"]) == "K1A 0B1"
    assert normalize_value("k1a  0b1", RULES["postal_code"]) == "K1A 0B1"
    assert validate_cell("K1A 0B1", RULES["postal_code"]) is None
    assert validate_cell("12345", RULES["postal_code"]) == (ERROR, "must match A1A 1A1")
    assert validate_cell("", RULES["postal_code"]) is None  # optional


def test_website_rule():
    assert validate_cell("https://988.ca", RULES["website"]) is None
    assert validate_cell("http://211.ca", RULES["website"]) is None
    assert validate_cell("988.ca", RULES["website"]) == (
        ERROR, "must start with http:// or https://"
    )


def test_float_ranges_use_wireframe_message():
    assert validate_cell("43.65", RULES["latitude"]) is None
    assert validate_cell("95", RULES["latitude"]) == (
        ERROR, "must be a number, \u221290 to 90"
    )
    assert validate_cell("abc", RULES["longitude"]) == (
        ERROR, "must be a number, \u2212180 to 180"
    )
    assert validate_cell("-179.9", RULES["longitude"]) is None


def test_enum_rules():
    assert validate_cell("Ontario", RULES["province_territory"]) is None
    assert validate_cell("Ontariooo", RULES["province_territory"])[0] == ERROR
    assert validate_cell("Poison Control", RULES["service_category"]) is None


def test_required_blank_is_warning_not_error():
    # phone_1: required, mixed formats, NO format validation, blanks
    # flagged but publish not blocked
    assert validate_cell("", RULES["phone_1"]) == (WARNING, "required — currently blank")
    assert validate_cell("211", RULES["phone_1"]) is None
    assert validate_cell("Toll-free: 1-800-265-3333; TTY: 1-866-323-4408",
                         RULES["phone_1"]) is None

    findings = validate_edits(
        {(1, "phone_1"): "", (1, "website"): "bad", (2, "name"): "  X  "}, RULES
    )
    assert list(errors_only(findings)) == [(1, "website")]
    assert list(warnings_only(findings)) == [(1, "phone_1")]


def test_audit_write_local_csv(tmp_path=None):
    tmp = tempfile.mkdtemp()
    try:
        shutil.copy(os.path.join(ROOT, "sample_data", "resources.csv"),
                    os.path.join(tmp, "resources.csv"))
        store = create_storage_provider(
            "local_csv", {"path": os.path.join(tmp, "resources.csv")}
        )
        df = store.load()
        rid = df.index[0]
        store.apply_edits(df, {(rid, "city_town"): "Toronto"})
        store.write_audit(
            {"last_updated_at": "2026-07-27T00:00:00+00:00",
             "last_updated_by": "admin@example.com"},
            [{"row_id": str(rid), "column": "city_town", "old_value": "X",
              "new_value": "Toronto", "timestamp": "2026-07-27T00:00:00+00:00",
              "user": "admin@example.com"}],
        )
        audit = os.path.join(tmp, "resources.csv.audit.jsonl")
        assert os.path.exists(audit)
        import json
        line = json.loads(open(audit).read().splitlines()[0])
        assert line["last_updated_by"] == "admin@example.com"
        assert line["records"][0]["column"] == "city_town"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
