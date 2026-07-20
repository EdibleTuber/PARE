import json
from pare.handback import normalize_class, candidate_classes, near_duplicate, disambig_question, spin_question

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


def test_candidate_classes_matches_qualified_smali_pattern():
    """Live regression (smoke test 1): gemma greps the FULLY-QUALIFIED pattern
    `Lsg/.../OMTG_DATAST_001_SQLite`, not the bare name — which never appeared as a
    substring of a class simple name, so disambiguation silently didn't fire. Every
    pattern form must yield the same variant set."""
    expected = {
        f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted",
        f"{PKG}.OMTG_DATAST_001_SQLite_Not_Encrypted",
    }
    for pat in (
        "OMTG_DATAST_001_SQLite",                     # bare
        f"{LPKG}/OMTG_DATAST_001_SQLite",            # smali-qualified, no trailing ;
        f"{LPKG}/OMTG_DATAST_001_SQLite;",           # smali-qualified with ;
        f"{PKG}.OMTG_DATAST_001_SQLite",             # dotted-qualified
    ):
        assert candidate_classes(_GREP_RESULT, pat) == expected, f"pattern {pat!r}"


def test_candidate_classes_empty_pattern_yields_nothing():
    """An empty stem must not match everything."""
    assert candidate_classes(_GREP_RESULT, "") == set()


def test_near_duplicate_arms_on_qualified_pattern():
    cands = {
        f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted",
        f"{PKG}.OMTG_DATAST_001_SQLite_Not_Encrypted",
    }
    assert near_duplicate(cands, f"{LPKG}/OMTG_DATAST_001_SQLite") is True
    assert near_duplicate(cands, f"{LPKG}/OMTG_DATAST_001_SQLite;") is True


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


def test_near_duplicate_arms_on_omtg_variants():
    cands = {f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted", f"{PKG}.OMTG_DATAST_001_SQLite_Not_Encrypted"}
    assert near_duplicate(cands, "OMTG_DATAST_001_SQLite") is True


def test_near_duplicate_does_not_arm_on_unrelated_shared_token():
    cands = {f"{PKG}.UserManager", f"{PKG}.UserActivity", f"{PKG}.UserRepository"}
    assert near_duplicate(cands, "User") is False


def test_near_duplicate_needs_at_least_two():
    assert near_duplicate({f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted"}, "OMTG_DATAST_001_SQLite") is False


def test_near_duplicate_does_not_arm_on_framework_classes():
    # candidate_classes may return framework classes (it's a dumb extractor);
    # near_duplicate is the gate that must reject them.
    assert near_duplicate({"android.database.sqlite.SQLiteDatabase"}, "SQLiteDatabase") is False  # lone
    cands = {"android.database.sqlite.SQLiteDatabase",
             "android.database.sqlite.SQLiteOpenHelper",
             "android.database.sqlite.SQLiteCursor"}
    assert near_duplicate(cands, "SQLite") is False  # share only a short prefix fragment


def test_questions_list_the_candidates():
    cands = {f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted", f"{PKG}.OMTG_DATAST_001_SQLite_Not_Encrypted"}
    q = disambig_question(f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted", cands)
    assert "OMTG_DATAST_001_SQLite_Not_Encrypted" in q and "?" in q
    s = spin_question("static_grep_smali", {"pattern": "X"}, 6, "0 matches", cands)
    assert "6" in s and "static_grep_smali" in s
