"""Tests for the cartulary validator.

These run against the neutral example schemas in ``examples/`` (a small
library catalogue of books and authors, plus a single-schema note format),
so the suite doubles as living documentation of every validator feature:
frontmatter typing, title patterns, section/subsection structure, typed
tables, reference lists, logs, list-valued fields, and — the headline
capability — cross-document reference resolution with reciprocal
(inverse) checking.
"""

from pathlib import Path

import pytest
import yaml
from marko import Markdown
from marko.ext.gfm import GFM

from cartulary import validate_file, validate_files
from cartulary.validator import (
    SchemaValidator,
    load_schema,
    split_frontmatter,
    build_section_tree,
    _extract_raw_title,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
LIBRARY = ROOT / "examples" / "library.schema.yaml"
NOTE = ROOT / "examples" / "note.schema.yaml"


# ── helpers ──────────────────────────────────────────────────

def parse(text: str):
    """Parse markdown text into (frontmatter, title, sections) like the validator does."""
    fm, body = split_frontmatter(text)
    title = _extract_raw_title(body)
    ast = Markdown(extensions=[GFM]).parse(body)
    _, sections = build_section_tree(ast)
    return fm, title, sections


def write(dir_: Path, name: str, text: str) -> str:
    p = dir_ / name
    p.write_text(text)
    return str(p)


def dump_schema(dir_: Path, name: str, schema: dict) -> str:
    p = dir_ / name
    p.write_text(yaml.safe_dump(schema))
    return str(p)


def paths(errors):
    return {e.path for e in errors}


# ── single-file validation against the example library schema ─

def test_valid_book_passes():
    errors = validate_file(str(LIBRARY), str(FIXTURES / "the-hobbit-1937.md"))
    assert errors == [], [f"[{e.path}] {e.message}" for e in errors]


def test_valid_author_passes():
    errors = validate_file(str(LIBRARY), str(FIXTURES / "jrr-tolkien.md"))
    assert errors == [], [f"[{e.path}] {e.message}" for e in errors]


def test_bad_book_catches_invalid_year():
    errors = validate_file(str(LIBRARY), str(FIXTURES / "bad-book-2099.md"))
    assert "frontmatter.year" in paths(errors)


def test_bad_book_catches_invalid_enum_status():
    errors = validate_file(str(LIBRARY), str(FIXTURES / "bad-book-2099.md"))
    assert "frontmatter.status" in paths(errors)


def test_bad_book_catches_title_mismatch():
    errors = validate_file(str(LIBRARY), str(FIXTURES / "bad-book-2099.md"))
    assert "title" in paths(errors)


def test_bad_book_catches_section_order():
    errors = validate_file(str(LIBRARY), str(FIXTURES / "bad-book-2099.md"))
    order = [e for e in errors if e.path == "sections" and "order" in e.message.lower()]
    assert order


def test_bad_book_catches_typed_table_cells():
    errors = validate_file(str(LIBRARY), str(FIXTURES / "bad-book-2099.md"))
    p = paths(errors)
    assert "section[Editions].row[0].Year" in p
    assert "section[Editions].row[0].Format" in p
    assert "section[Editions].row[0].ISBN" in p


def test_bad_book_error_count_is_exactly_seven():
    errors = validate_file(str(LIBRARY), str(FIXTURES / "bad-book-2099.md"))
    assert len(errors) == 7, [f"[{e.path}] {e.message}" for e in errors]


# ── multi-schema routing ─────────────────────────────────────

def test_unknown_document_type_reported(tmp_path):
    fp = write(tmp_path, "x.md", "---\ndocument_type: magazine\n---\n\n# X\n")
    errors = validate_file(str(LIBRARY), fp)
    assert any(e.path == "frontmatter.document_type" for e in errors)


def test_missing_document_type_reported(tmp_path):
    fp = write(tmp_path, "x.md", "---\ntitle: X\n---\n\n# X\n")
    results = validate_files(str(LIBRARY), [fp])
    assert any(e.path == "frontmatter.document_type" for e in results[fp])


# ── cross-document reference integrity (the headline feature) ─

VALID_CORPUS = ["the-hobbit-1937.md", "the-silmarillion-1977.md", "jrr-tolkien.md"]


def test_valid_corpus_has_no_errors():
    files = [str(FIXTURES / n) for n in VALID_CORPUS]
    results = validate_files(str(LIBRARY), files)
    flat = [f"[{fp}] [{e.path}] {e.message}" for fp, errs in results.items() for e in errs]
    assert flat == [], flat


def test_unresolved_reference_reported(tmp_path):
    orphan = write(tmp_path, "orphan-ref-2000.md", _BOOK_REFERENCING.format(ref="ghost-author"))
    files = [str(FIXTURES / n) for n in VALID_CORPUS] + [orphan]
    results = validate_files(str(LIBRARY), files)
    assert any("Unresolved reference" in e.message for e in results[orphan])


def test_missing_reciprocal_reference_reported(tmp_path):
    # A book that references Tolkien, but Tolkien's file does not list it back.
    extra = write(tmp_path, "unfinished-tales-1980.md", _BOOK_REFERENCING.format(ref="jrr-tolkien"))
    files = [str(FIXTURES / n) for n in VALID_CORPUS] + [extra]
    results = validate_files(str(LIBRARY), files)
    tolkien_fp = str(FIXTURES / "jrr-tolkien.md")
    recip = [e for e in results[tolkien_fp] if "reciprocal" in e.message.lower()]
    assert recip, [f"[{e.path}] {e.message}" for e in results[tolkien_fp]]
    assert recip[0].severity == "warning"


def test_reciprocal_reference_satisfied_is_clean():
    # The valid corpus reciprocates fully — covered by test_valid_corpus_has_no_errors,
    # asserted here directly on Tolkien's file for clarity.
    files = [str(FIXTURES / n) for n in VALID_CORPUS]
    results = validate_files(str(LIBRARY), files)
    assert results[str(FIXTURES / "jrr-tolkien.md")] == []


_BOOK_REFERENCING = """---
document_type: book
book_id: {{book_id}}
title: Some Title
year: "1980"
status: in-print
---

# Some Title (1980)

## Summary

A book used in reference-integrity tests.

## Written By

- An Author → `{ref}`

## Change Log

- 2026-01-10: Catalogued.
"""


def _book_referencing(book_id: str, ref: str) -> str:
    return _BOOK_REFERENCING.replace("{{book_id}}", book_id).format(ref=ref)


def test_duplicate_primary_key_reported(tmp_path):
    schema = {
        "primary_key": "id",
        "frontmatter": {"fields": {"id": {"required": True, "primary_key": True}}},
        "sections": [],
        "additional_sections": True,
    }
    sp = dump_schema(tmp_path, "s.yaml", schema)
    a = write(tmp_path, "a.md", "---\nid: shared\n---\n")
    b = write(tmp_path, "b.md", "---\nid: shared\n---\n")
    results = validate_files(sp, [a, b])
    dupes = [e for errs in results.values() for e in errs if "Duplicate primary key" in e.message]
    assert dupes


# ── filename_must_match & file existence ─────────────────────

def test_filename_must_match(tmp_path):
    fp = write(tmp_path, "wrong-name-2001.md", _book_referencing("the-right-id-2001", "jrr-tolkien"))
    errors = validate_file(str(LIBRARY), fp)
    assert any(e.path == "filename" for e in errors)


def test_cover_exists_warns_when_missing(tmp_path):
    text = """---
document_type: book
book_id: missing-cover-2010
title: Missing Cover
year: "2010"
status: in-print
cover: covers/nope.txt
---

# Missing Cover (2010)

## Summary

No cover file on disk.

## Written By

- An Author → `jrr-tolkien`

## Change Log

- 2026-01-10: Catalogued.
"""
    fp = write(tmp_path, "missing-cover-2010.md", text)
    errors = validate_file(str(LIBRARY), fp)
    cover_errs = [e for e in errors if e.path == "frontmatter.cover"]
    assert cover_errs and cover_errs[0].severity == "warning"


# ── single-schema example (note.schema.yaml) ─────────────────

VALID_NOTE = """---
note_id: standup-notes
title: Standup Notes
priority: high
tags:
  - meeting
  - team
links:
  - some-related-note
  - label: Project board
    url: https://example.test/board
---

# Standup Notes

## Body

Notes from the daily standup.
"""


def test_valid_note_passes(tmp_path):
    fp = write(tmp_path, "standup-notes.md", VALID_NOTE)
    errors = validate_file(str(NOTE), fp)
    assert errors == [], [f"[{e.path}] {e.message}" for e in errors]


def test_note_list_item_type_checked(tmp_path):
    bad = VALID_NOTE.replace("- meeting", "- Not A Slug!")
    fp = write(tmp_path, "standup-notes.md", bad)
    errors = validate_file(str(NOTE), fp)
    assert any(e.path.startswith("frontmatter.tags[") for e in errors)


def test_note_any_of_object_option(tmp_path):
    # A links item missing the required `url` should fail the object option,
    # and (being a dict) cannot match the scalar slug option either.
    bad = VALID_NOTE.replace("    url: https://example.test/board\n", "")
    fp = write(tmp_path, "standup-notes.md", bad)
    errors = validate_file(str(NOTE), fp)
    assert any(e.path.startswith("frontmatter.links[") for e in errors)


def test_note_additional_section_warns(tmp_path):
    text = VALID_NOTE + "\n## Appendix\n\nExtra.\n"
    fp = write(tmp_path, "standup-notes.md", text)
    errors = validate_file(str(NOTE), fp)
    unknown = [e for e in errors if "Unknown section" in e.message]
    assert unknown and unknown[0].severity == "warning"


# ── schema loading ───────────────────────────────────────────

def test_load_schema_detects_multi():
    loaded = load_schema(str(LIBRARY))
    assert loaded.get("_multi")
    assert set(loaded["schemas"]) == {"book", "author"}


def test_load_schema_propagates_shared_value_types():
    loaded = load_schema(str(LIBRARY))
    # `year` is declared once at the top level; both sub-schemas should see it.
    assert "year" in loaded["schemas"]["book"]["value_types"]
    assert "year" in loaded["schemas"]["author"]["value_types"]


def test_load_schema_single_is_not_multi():
    loaded = load_schema(str(NOTE))
    assert not loaded.get("_multi")
    assert "frontmatter" in loaded


# ── focused unit tests on inline schemas ─────────────────────

def test_required_frontmatter_field_missing():
    schema = {"frontmatter": {"fields": {"id": {"required": True}}}, "sections": []}
    v = SchemaValidator(schema)
    errors = v.validate({}, None, [])
    assert any(e.path == "frontmatter.id" for e in errors)


def test_enum_field_rejected():
    schema = {"frontmatter": {"fields": {"size": {"enum": ["s", "m", "l"]}}}, "sections": []}
    v = SchemaValidator(schema)
    errors = v.validate({"size": "xl"}, None, [])
    assert any(e.path == "frontmatter.size" for e in errors)


def test_deprecated_section_warns():
    schema = {
        "frontmatter": {"fields": {}},
        "sections": [{"heading": "Old", "deprecated": True}],
        "additional_sections": True,
    }
    fm, title, sections = parse("# T\n\n## Old\n\nstuff\n")
    errors = SchemaValidator(schema).validate(fm, title, sections)
    dep = [e for e in errors if "Deprecated" in e.message]
    assert dep and dep[0].severity == "warning"


def test_subsection_required_and_order():
    schema = {
        "frontmatter": {"fields": {}},
        "additional_sections": True,
        "sections": [{
            "heading": "Parent",
            "subsections": [
                {"heading": "First", "required": True},
                {"heading": "Second", "required": True},
            ],
        }],
    }
    # Subsections present but in the wrong order.
    fm, title, sections = parse("# T\n\n## Parent\n\n### Second\n\nx\n\n### First\n\ny\n")
    errors = SchemaValidator(schema).validate(fm, title, sections)
    assert any("order" in e.message.lower() for e in errors)


def test_log_entry_pattern():
    schema = {
        "frontmatter": {"fields": {}},
        "additional_sections": True,
        "sections": [{
            "heading": "Log",
            "content": {"type": "log", "entry_pattern": r"^\d{4}-\d{2}-\d{2}: .+"},
        }],
    }
    fm, title, sections = parse("# T\n\n## Log\n\n- not a dated entry\n")
    errors = SchemaValidator(schema).validate(fm, title, sections)
    assert any(e.path.startswith("section[Log].entry[") for e in errors)


def test_labeled_ref_list_collects_ref_and_inverse():
    schema = {
        "frontmatter": {"fields": {"id": {"required": True, "primary_key": True}}},
        "additional_sections": True,
        "sections": [{
            "heading": "Links",
            "content": {
                "type": "ref_list",
                "style": "labeled",
                "items": [{"label": "Parent", "inverse": "Children"}],
            },
        }],
    }
    fm, title, sections = parse("# T\n\n## Links\n\n- **Parent:** Foo → `foo-1`\n")
    v = SchemaValidator(schema)
    v.validate(fm, title, sections)
    assert "foo-1" in [ref for _, ref, _ in v.refs_found]
    assert ("Links", "foo-1", "Children") in v.inverse_refs


def test_table_min_rows_enforced():
    schema = {
        "frontmatter": {"fields": {}},
        "additional_sections": True,
        "sections": [{
            "heading": "Rows",
            "content": {"type": "table", "min_rows": 2, "columns": {"A": {}}},
        }],
    }
    fm, title, sections = parse("# T\n\n## Rows\n\n| A |\n|---|\n| x |\n")
    errors = SchemaValidator(schema).validate(fm, title, sections)
    assert any("at least 2" in e.message for e in errors)


def test_circa_marker_allowed_in_title():
    # The validator permits an optional '~' before substituted title values
    # (a generic "approximately" convention).
    schema = {
        "frontmatter": {"fields": {"year": {}}},
        "title_pattern": "Thing {year}",
        "sections": [],
    }
    fm, title, sections = parse("---\nyear: 1850\n---\n\n# Thing ~1850\n")
    errors = SchemaValidator(schema).validate(fm, title, sections)
    assert [e for e in errors if e.path == "title"] == []


# ── #1: references must resolve to the right document type ───

FT = ROOT / "examples" / "family-tree.schema.yaml"
FT_DIR = ROOT / "examples" / "family-tree"
FT_CORPUS = [
    "bungo-baggins.md", "belladonna-took.md", "bilbo-baggins.md",
    "drogo-baggins.md", "primula-brandybuck.md", "frodo-baggins.md", "red-book.md",
]

_BOOK_WITH_AUTHOR_REF = """---
document_type: book
book_id: {book_id}
title: Bad Ref
year: "2020"
status: in-print
---

# Bad Ref (2020)

## Summary

A book whose author reference points at the wrong kind of document.

## Written By

- {label} → `{ref}`

## Change Log

- 2026-01-12: Catalogued.
"""


def test_ref_resolving_to_wrong_document_type_is_an_error(tmp_path):
    # `ref: author_id` must point at an author; here it points at a *book*.
    book = write(tmp_path, "bad-ref-2020.md", _BOOK_WITH_AUTHOR_REF.format(
        book_id="bad-ref-2020", label="The Hobbit", ref="the-hobbit-1937"))
    files = [str(FIXTURES / n) for n in VALID_CORPUS] + [book]
    results = validate_files(str(LIBRARY), files)
    bad = [e for e in results[book] if "the-hobbit-1937" in e.message and "author" in e.message.lower()]
    assert bad, [f"[{e.path}] {e.message}" for e in results[book]]
    assert bad[0].severity == "error"


def test_ref_resolving_to_correct_type_is_clean(tmp_path):
    book = write(tmp_path, "good-ref-2020.md", _BOOK_WITH_AUTHOR_REF.format(
        book_id="good-ref-2020", label="J.R.R. Tolkien", ref="jrr-tolkien"))
    files = [str(FIXTURES / n) for n in VALID_CORPUS] + [book]
    results = validate_files(str(LIBRARY), files)
    assert not any("wrong" in e.message.lower() or "expected" in e.message.lower()
                   for e in results[book])


def test_family_tree_corpus_is_clean():
    files = [str(FT_DIR / n) for n in FT_CORPUS]
    results = validate_files(str(FT), files)
    flat = [f"[{fp}] [{e.path}] {e.message}" for fp, errs in results.items() for e in errs]
    assert flat == [], flat


def test_parent_must_be_a_person_not_a_source(tmp_path):
    # Make Bilbo list the Red Book (a `source`) as a parent.
    bad = (FT_DIR / "bilbo-baggins.md").read_text().replace(
        "- Bungo Baggins → `bungo-baggins`", "- The Red Book → `red-book`")
    fp = write(tmp_path, "bilbo-baggins.md", bad)
    files = [str(FT_DIR / n) for n in FT_CORPUS if n != "bilbo-baggins.md"] + [fp]
    results = validate_files(str(FT), files)
    typ = [e for e in results[fp] if "red-book" in e.message and "person" in e.message.lower()]
    assert typ, [f"[{e.path}] {e.message}" for e in results[fp]]


# ── #5: malformed YAML frontmatter is surfaced, not swallowed ─

def test_malformed_yaml_frontmatter_reported_validate_files(tmp_path):
    fp = write(tmp_path, "broken.md", "---\nkey: [1, 2\n---\n\n# X\n")
    results = validate_files(str(LIBRARY), [fp])
    assert any("Invalid YAML frontmatter" in e.message for e in results[fp])


def test_malformed_yaml_frontmatter_reported_validate_file(tmp_path):
    fp = write(tmp_path, "broken.md", "---\nkey: [1, 2\n---\n\n# X\n")
    errors = validate_file(str(LIBRARY), fp)
    assert any("Invalid YAML frontmatter" in e.message for e in errors)


# ── #4: machine-readable JSON output ─────────────────────────

def test_json_output_is_empty_for_clean_corpus():
    import json
    from cartulary.validator import results_to_json
    files = [str(FIXTURES / n) for n in VALID_CORPUS]
    payload = json.loads(results_to_json(validate_files(str(LIBRARY), files)))
    assert payload == []


def test_json_output_findings_have_expected_keys():
    import json
    from cartulary.validator import results_to_json
    results = validate_files(str(LIBRARY), [str(FIXTURES / "bad-book-2099.md")])
    payload = json.loads(results_to_json(results))
    assert payload, "expected findings for a bad file"
    assert {"file", "path", "message", "severity"} <= set(payload[0])


# ── schema meta-validation (validating the schema itself) ────

from cartulary import validate_schema  # noqa: E402

ALL_SCHEMAS = sorted((ROOT / "examples").glob("*.schema.yaml")) + \
    sorted((ROOT / "conformance" / "schemas").glob("*.yaml"))


@pytest.mark.parametrize("schema_path", ALL_SCHEMAS, ids=[p.name for p in ALL_SCHEMAS])
def test_shipped_schemas_are_clean(schema_path):
    findings = validate_schema(str(schema_path))
    assert findings == [], [f"[{f.severity}] {f.path}: {f.message}" for f in findings]


def test_meta_unknown_top_level_key_warns():
    findings = validate_schema({"sektions": [], "frontmatter": {"fields": {}}})
    assert any(f.severity == "warning" and "sektions" in f.message for f in findings)


def test_meta_undefined_value_type_is_error():
    schema = {"frontmatter": {"fields": {"x": {"type": "nope"}}}}
    findings = validate_schema(schema)
    assert any(f.severity == "error" and "nope" in f.message for f in findings)


def test_meta_unknown_content_type_is_error():
    schema = {"sections": [{"heading": "S", "content": {"type": "tabel"}}]}
    findings = validate_schema(schema)
    assert any(f.severity == "error" and "tabel" in f.message for f in findings)


def test_meta_primary_key_must_name_a_field():
    schema = {"primary_key": "missing", "frontmatter": {"fields": {"id": {}}}}
    findings = validate_schema(schema)
    assert any(f.severity == "error" and f.path.endswith("primary_key") for f in findings)


def test_meta_filename_must_match_must_name_a_field():
    schema = {"filename_must_match": "nope", "frontmatter": {"fields": {"id": {}}}}
    findings = validate_schema(schema)
    assert any(f.severity == "error" and "filename_must_match" in f.path for f in findings)


def test_meta_misspelled_field_key_warns():
    # The classic footgun: `requried` instead of `required` silently does nothing.
    schema = {"frontmatter": {"fields": {"id": {"requried": True}}}}
    findings = validate_schema(schema)
    assert any(f.severity == "warning" and "requried" in f.message for f in findings)


def test_meta_multi_schema_subschema_path():
    schema = {"schemas": {"book": {"frontmatter": {"fields": {"x": {"type": "nope"}}}}}}
    findings = validate_schema(schema)
    assert any("schema:book" in f.path and "nope" in f.message for f in findings)
