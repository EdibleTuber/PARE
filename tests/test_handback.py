import json
from pare.handback import normalize_class, candidate_classes

PKG = "sg.vp.owasp_mobile.OMTG_Android"
LPKG = "Lsg/vp/owasp_mobile/OMTG_Android"

# Real capture rows: class column is the MyActivity dispatcher; the variant is the
# const-class type token in `insn`.
_ROWS = [
    {"class": f"{LPKG}/MyActivity;", "method": "OMTG_DATAST_001_SQLite_Encrypted",
     "insn": f"const-class v0, {LPKG}/OMTG_DATAST_001_SQLite_Encrypted;", "match": "OMTG_DATAST"},
    {"class": f"{LPKG}/MyActivity;", "method": "OMTG_DATAST_001_SQLite_Not_Encrypted",
     "insn": f"const-class v0, {LPKG}/OMTG_DATAST_001_SQLite_Not_Encrypted;", "match": "OMTG_DATAST"},
]
_GREP_RESULT = json.dumps({"summary": "grep_smali: 2 row(s)", "package": PKG.lower(), "rows": _ROWS})


def test_normalize_class_smali_to_dotted():
    assert normalize_class(f"{LPKG}/OMTG_DATAST_001_SQLite_Encrypted;") == f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted"
    assert normalize_class(f"{PKG}.Foo") == f"{PKG}.Foo"  # dotted passes through


def test_candidate_classes_from_referenced_type_not_class_column():
    got = candidate_classes(_GREP_RESULT, "OMTG_DATAST_001_SQLite")
    assert got == {
        f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted",
        f"{PKG}.OMTG_DATAST_001_SQLite_Not_Encrypted",
    }
    # MyActivity (the class column / dispatcher) must NOT be a candidate
    assert not any("MyActivity" in c for c in got)


def test_candidate_classes_is_a_dumb_extractor_framework_noise_filtered_downstream():
    # a grep whose only class token is a framework class named like the pattern
    rows = [{"class": f"{LPKG}/Foo;", "method": "m",
             "insn": "invoke-virtual v0, Landroid/database/sqlite/SQLiteDatabase;->rawQuery", "match": "SQLiteDatabase"}]
    res = json.dumps({"rows": rows})
    # extraction is dumb; a lone framework class never arms disambiguation — the near_duplicate >=2 gate (Task 3) filters it.
    assert candidate_classes(res, "SQLiteDatabase") == {"android.database.sqlite.SQLiteDatabase"}


def test_candidate_classes_keeps_exact_name_app_class():
    rows = [{"class": f"{LPKG}/MyActivity;", "method": "start",
             "insn": f"const-class v0, {LPKG}/MainActivity;", "match": "MainActivity"}]
    res = json.dumps({"rows": rows})
    assert candidate_classes(res, "MainActivity") == {f"{PKG}.MainActivity"}


def test_candidate_classes_reads_capture_stub_when_ref_present():
    class _Store:
        def get(self, ref): return {"body": _GREP_RESULT}
    stub = json.dumps({"summary": "grep_smali: 2 row(s)", "captured": {"ref": "abc"}, "hint": "read_capture"})
    got = candidate_classes(stub, "OMTG_DATAST_001_SQLite", capture_store=_Store())
    assert f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted" in got
