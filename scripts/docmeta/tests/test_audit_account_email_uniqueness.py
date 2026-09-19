import json
import subprocess
import sys

def run_script(tmp_path, lines, extra_args=None):
    jsonl_file = tmp_path / "test.jsonl"
    with open(jsonl_file, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
            
    cmd = [sys.executable, "-m", "scripts.docmeta.audit_account_email_uniqueness", "--accounts-jsonl", str(jsonl_file)]
    if extra_args:
        cmd.extend(extra_args)
        
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    output = None
    if result.stdout.strip():
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            pass
            
    return result.returncode, output, result.stderr

def run_script_no_input():
    cmd = [sys.executable, "-m", "scripts.docmeta.audit_account_email_uniqueness"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stderr

def test_no_input_fails():
    code, err = run_script_no_input()
    assert code != 0
    assert "required" in err.lower() or "too few arguments" in err.lower()

def test_case_only_duplicate(tmp_path):
    lines = [
        json.dumps({"id": "1", "email": "ALEX@example.org"}),
        json.dumps({"id": "2", "email": "alex@example.org"}),
    ]
    code, out, err = run_script(tmp_path, lines)
    assert code == 0
    assert out["summary"]["duplicate_current_runtime_key_groups"] == 1
    assert out["summary"]["duplicate_proposed_constraint_key_groups"] == 1
    
    group_curr = out["duplicate_groups"]["current_runtime_key"][0]
    assert group_curr["key"] == "alex@example.org"
    ids = [i["id"] for i in group_curr["items"]]
    assert ids == ["1", "2"]
    
    group_prop = out["duplicate_groups"]["proposed_constraint_key"][0]
    assert group_prop["key"] == "alex@example.org"
    
    classes_by_id = {item["id"]: item["classifications"] for item in group_curr["items"]}
    assert "case_changes_value" in classes_by_id["1"]
    assert "valid_email" in classes_by_id["1"]
    assert "valid_email" in classes_by_id["2"]

def test_whitespace_sensitive_duplicate(tmp_path):
    lines = [
        json.dumps({"id": "1", "email": " alex@example.org "}),
        json.dumps({"id": "2", "email": "alex@example.org"}),
    ]
    code, out, err = run_script(tmp_path, lines)
    assert code == 0
    # current runtime key does not trim, so " alex@example.org " and "alex@example.org" are different!
    assert out["summary"]["duplicate_current_runtime_key_groups"] == 0
    # proposed trims, so they are duplicates
    assert out["summary"]["duplicate_proposed_constraint_key_groups"] == 1
    assert len(out["duplicate_groups"]["current_runtime_key"]) == 0
    
    group = out["duplicate_groups"]["proposed_constraint_key"][0]
    assert group["key"] == "alex@example.org"

    classes_by_id = {item["id"]: item["classifications"] for item in group["items"]}
    assert "trim_changes_value" in classes_by_id["1"]
    assert "valid_email" in classes_by_id["1"]
    assert "valid_email" in classes_by_id["2"]

def test_missing_null_empty(tmp_path):
    lines = [
        json.dumps({"id": "1"}), # missing
        json.dumps({"id": "2", "email": None}), # null
        json.dumps({"id": "3", "email": ""}), # empty
        json.dumps({"id": "4", "email": "   "}), # empty after trim
    ]
    code, out, err = run_script(tmp_path, lines)
    assert code == 0
    assert out["summary"]["records_missing_email"] == 1
    assert out["summary"]["records_null_email"] == 1
    assert out["summary"]["records_empty_after_trim"] == 2

def test_deterministic_ordering(tmp_path):
    lines = [
        json.dumps({"id": "2", "email": "alex@example.org"}),
        json.dumps({"id": "1", "email": "ALEX@example.org"}),
    ]
    code, out, err = run_script(tmp_path, lines)
    assert code == 0
    group = out["duplicate_groups"]["proposed_constraint_key"][0]
    ids = [i["id"] for i in group["items"]]
    # Even though "2" was first in input, "1" should be first in output because of deterministic sorting
    assert ids == ["1", "2"]

def test_fail_on_duplicates(tmp_path):
    lines = [
        json.dumps({"id": "1", "email": "alex@example.org"}),
        json.dumps({"id": "2", "email": "alex@example.org"}),
    ]
    code, out, err = run_script(tmp_path, lines, ["--fail-on-duplicates"])
    assert code == 1

def test_fail_on_duplicates_success(tmp_path):
    lines = [
        json.dumps({"id": "1", "email": "a@example.org"}),
        json.dumps({"id": "2", "email": "b@example.org"}),
    ]
    code, out, err = run_script(tmp_path, lines, ["--fail-on-duplicates"])
    assert code == 0

def test_malformed_jsonl(tmp_path):
    lines = [
        '{"id": "1", "email": "a@example.org"}',
        'invalid json',
        '{"id": "2", "email": "b@example.org"}'
    ]
    code, out, err = run_script(tmp_path, lines)
    assert code == 0
    assert out["summary"]["records_invalid_json"] == 1
    invalid_findings = [f for f in out["findings"] if "invalid_json" in f.get("classifications", [])]
    assert len(invalid_findings) == 1

def test_ascii_only_lower(tmp_path):
    # 'İ' is I with dot above, 'I' is I. If we use python .lower(), 'İ' becomes 'i\u0307'
    # we want only A-Z to be lowered
    lines = [
        json.dumps({"id": "1", "email": "ALEX-ß-İ-I@example.org"}),
        json.dumps({"id": "2", "email": "alex-ß-İ-i@example.org"}),
    ]
    code, out, err = run_script(tmp_path, lines)
    assert code == 0
    assert out["summary"]["duplicate_current_runtime_key_groups"] == 1
    findings_1 = [f for f in out["findings"] if f["id"] == "1"]
    assert len(findings_1) == 1
    finding = findings_1[0]
    assert finding["current_runtime_key"] == "alex-ß-İ-i@example.org"
    assert finding["proposed_constraint_key"] == "alex-ß-İ-i@example.org"

def test_valid_conflict_free_not_in_findings(tmp_path):
    lines = [
        json.dumps({"id": "1", "email": "valid@example.org"}),
        json.dumps({"id": "2", "email": "also-valid@example.org"}),
    ]
    code, out, err = run_script(tmp_path, lines)
    assert code == 0
    # No findings should exist for valid emails without conflicts
    assert len(out["findings"]) == 0
    assert out["summary"]["records_with_email"] == 2
    
def test_missing_id_and_non_string_email(tmp_path):
    lines = [
        json.dumps({"email": "valid@example.org"}), # missing id
        json.dumps({"id": "2", "email": {"complex": "object"}}), # non-string
        json.dumps({"id": "3", "email": 12345}), # non-string
    ]
    code, out, err = run_script(tmp_path, lines)
    assert code == 0
    assert out["summary"]["records_missing_id"] == 1
    assert out["summary"]["records_non_string_email"] == 2
    
    findings = out["findings"]
    assert len(findings) == 3
    
    missing_id_findings = [
        f for f in findings
        if "missing_id" in f.get("classifications", [])
    ]
    assert len(missing_id_findings) == 1
    
    non_strings = [f for f in findings if "non_string_email" in f["classifications"]]
    assert len(non_strings) == 2

def test_non_object_json(tmp_path):
    lines = [
        json.dumps(["not", "an", "object"]),
        json.dumps({"id": "1", "email": "a@example.org"}),
    ]
    code, out, err = run_script(tmp_path, lines)
    assert code == 0
    assert out["summary"]["records_non_object_json"] == 1

    findings = [
        f for f in out["findings"]
        if "non_object_json" in f.get("classifications", [])
    ]
    assert len(findings) == 1

def test_unicode_output_is_not_ascii_escaped_with_duplicate(tmp_path):
    jsonl_file = tmp_path / "test.jsonl"
    jsonl_file.write_text(
        json.dumps({"id": "1", "email": "ALEX-ß-İ-I@example.org"}) + "\n" +
        json.dumps({"id": "2", "email": "alex-ß-İ-i@example.org"}) + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.docmeta.audit_account_email_uniqueness",
            "--accounts-jsonl",
            str(jsonl_file),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "ß" in result.stdout
    assert "İ" in result.stdout

def test_empty_whitespace_grouping(tmp_path):
    lines = [
        json.dumps({"id": "1", "email": ""}),
        json.dumps({"id": "2", "email": ""}),
        json.dumps({"id": "3", "email": "   "}),
        json.dumps({"id": "4", "email": "   "}),
    ]
    code, out, err = run_script(tmp_path, lines)
    assert code == 0
    assert out["summary"]["duplicate_current_runtime_key_groups"] == 2
    assert out["summary"]["duplicate_proposed_constraint_key_groups"] == 0

    current_keys = {
        group["key"]
        for group in out["duplicate_groups"]["current_runtime_key"]
    }
    assert "" in current_keys
    assert "   " in current_keys
    assert out["duplicate_groups"]["proposed_constraint_key"] == []

def test_same_id_no_duplicate(tmp_path):
    lines = [
        json.dumps({"id": "1", "email": "a@example.org"}),
        json.dumps({"id": "1", "email": "a@example.org"}), # same ID
    ]
    code, out, err = run_script(tmp_path, lines, ["--fail-on-duplicates"])
    assert code == 0 # Should not fail because distinct ID count is 1
    assert out["summary"]["duplicate_current_runtime_key_groups"] == 0
    assert out["summary"]["duplicate_proposed_constraint_key_groups"] == 0

def test_non_string_id(tmp_path):
    lines = [
        json.dumps({"id": 123, "email": "a@example.org"}),
        json.dumps({"id": 456, "email": "a@example.org"}),
    ]
    code, out, err = run_script(tmp_path, lines, ["--fail-on-duplicates"])
    assert code == 0
    assert out["summary"]["records_non_string_id"] == 2
    assert out["summary"]["duplicate_current_runtime_key_groups"] == 0
    assert out["summary"]["duplicate_proposed_constraint_key_groups"] == 0
    
    non_string_id_findings = [
        f for f in out["findings"]
        if "non_string_id" in f.get("classifications", [])
    ]
    assert len(non_string_id_findings) == 2
