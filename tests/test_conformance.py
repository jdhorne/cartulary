"""Runs the language-neutral conformance suite against the reference implementation.

Each case in ``conformance/cases/*.yaml`` is pure data: a schema (by filename
in ``conformance/schemas/``), one or more Markdown documents, and the expected
findings. The portable contract is the set of ``(document, path, severity)``
findings — exact wording is implementation-private and only checked here via
optional ``message_contains`` substrings. Any other-language implementation can
read the same files and assert the same contract. See ``conformance/README.md``.
"""

import pathlib

import pytest
import yaml

from cartulary import validate_files

ROOT = pathlib.Path(__file__).resolve().parents[1]
SUITE = ROOT / "conformance"


def _load_cases():
    params = []
    for case_file in sorted((SUITE / "cases").glob("*.yaml")):
        data = yaml.safe_load(case_file.read_text())
        cases = data if isinstance(data, list) else [data]
        for i, case in enumerate(cases):
            params.append(pytest.param(case, id=f"{case_file.stem}:{case.get('name', i)}"))
    return params


@pytest.mark.parametrize("case", _load_cases())
def test_conformance(case, tmp_path):
    schema_path = SUITE / "schemas" / case["schema"]
    assert schema_path.exists(), f"case references unknown schema {case['schema']!r}"

    doc_paths = []
    for doc in case["documents"]:
        fp = tmp_path / doc["path"]
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(doc["content"])
        doc_paths.append(str(fp))

    results = validate_files(str(schema_path), doc_paths)

    actual = sorted(
        (pathlib.Path(fp).name, e.path, e.severity)
        for fp, errs in results.items() for e in errs
    )
    expected = sorted(
        (e["file"], e["path"], e.get("severity", "error"))
        for e in case.get("expect", [])
    )
    assert actual == expected, (
        f"\ncase '{case.get('name')}' findings mismatch:"
        f"\n  expected: {expected}"
        f"\n  actual:   {actual}"
    )

    # Reference-implementation-only: optional message substring checks.
    for exp in case.get("expect", []):
        sub = exp.get("message_contains")
        if not sub:
            continue
        matches = [
            e.message
            for fp, errs in results.items() for e in errs
            if pathlib.Path(fp).name == exp["file"]
            and e.path == exp["path"]
            and e.severity == exp.get("severity", "error")
        ]
        assert any(sub in m for m in matches), (
            f"case '{case.get('name')}': no finding at {exp['file']}:{exp['path']} "
            f"contains {sub!r}; messages were {matches}"
        )
