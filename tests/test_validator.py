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

from cartulary import validate_file, validate_files, scope_to_changed
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


def test_meta_unknown_top_level_key_is_error():
    findings = validate_schema({"sektions": [], "frontmatter": {"fields": {}}})
    assert any(f.severity == "error" and "sektions" in f.message for f in findings)


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


def test_meta_misspelled_field_key_is_error():
    # The classic footgun: `requried` instead of `required` silently does nothing.
    # It must be an *error* — a misspelled key means the intended rule never runs.
    schema = {"frontmatter": {"fields": {"id": {"requried": True}}}}
    findings = validate_schema(schema)
    assert any(f.severity == "error" and "requried" in f.message for f in findings)


def test_meta_multi_schema_subschema_path():
    schema = {"schemas": {"book": {"frontmatter": {"fields": {"x": {"type": "nope"}}}}}}
    findings = validate_schema(schema)
    assert any("schema:book" in f.path and "nope" in f.message for f in findings)


# ── ref may target a document type (decoupled from PK field naming) ──

def _shared_pk_schema():
    """Two document types that deliberately share the PK field name `id`."""
    def kind(name):
        return {
            "frontmatter": {"fields": {
                "document_type": {"value": name},
                "id": {"required": True, "primary_key": True}}},
            "additional_sections": True,
            "sections": [{"heading": "Related", "content": {
                "type": "ref_list", "style": "unlabeled", "ref": "widget"}}],
        }
    return {"schemas": {"widget": kind("widget"), "gadget": kind("gadget")}}


def test_ref_targets_document_type_even_with_shared_pk_field(tmp_path):
    sp = dump_schema(tmp_path, "s.yaml", _shared_pk_schema())
    w = write(tmp_path, "w1.md",
              "---\ndocument_type: widget\nid: w1\n---\n\n# w1\n\n## Related\n\n- G → `g1`\n")
    g = write(tmp_path, "g1.md", "---\ndocument_type: gadget\nid: g1\n---\n\n# g1\n")
    results = validate_files(sp, [w, g])
    # `ref: widget` must reject a gadget, despite both types using PK field `id`.
    assert any("widget" in e.message and "gadget" in e.message for e in results[w]), \
        [f"{e.path}: {e.message}" for e in results[w]]


def test_ref_targets_document_type_correct_is_clean(tmp_path):
    sp = dump_schema(tmp_path, "s.yaml", _shared_pk_schema())
    w = write(tmp_path, "w1.md",
              "---\ndocument_type: widget\nid: w1\n---\n\n# w1\n\n## Related\n\n- W → `w2`\n")
    w2 = write(tmp_path, "w2.md", "---\ndocument_type: widget\nid: w2\n---\n\n# w2\n")
    results = validate_files(sp, [w, w2])
    assert not any("expected" in e.message.lower() or "resolves to" in e.message.lower()
                   for e in results[w]), [f"{e.path}: {e.message}" for e in results[w]]


# ── reference-list cardinality (max_items) ───────────────────

def test_ref_list_max_items_enforced():
    schema = {"frontmatter": {"fields": {}}, "additional_sections": True,
              "sections": [{"heading": "Parents", "content": {
                  "type": "ref_list", "style": "unlabeled", "ref": "id",
                  "min_items": 0, "max_items": 2}}]}
    fm, title, sections = parse(
        "# T\n\n## Parents\n\n- A → `p1`\n- B → `p2`\n- C → `p3`\n")
    errors = SchemaValidator(schema).validate(fm, title, sections)
    assert any("at most 2" in e.message.lower() for e in errors), \
        [e.message for e in errors]


def test_ref_list_within_max_items_is_clean():
    schema = {"frontmatter": {"fields": {}}, "additional_sections": True,
              "sections": [{"heading": "Parents", "content": {
                  "type": "ref_list", "style": "unlabeled", "ref": "id",
                  "min_items": 0, "max_items": 2}}]}
    fm, title, sections = parse("# T\n\n## Parents\n\n- A → `p1`\n- B → `p2`\n")
    errors = SchemaValidator(schema).validate(fm, title, sections)
    assert not any("most" in e.message.lower() for e in errors)


# ── strict frontmatter (additional_fields) ──────────────────

def test_strict_frontmatter_rejects_unknown_field():
    schema = {"additional_fields": False,
              "frontmatter": {"fields": {"id": {"required": True}}}, "sections": []}
    errors = SchemaValidator(schema).validate({"id": "x", "colour": "blue"}, None, [])
    assert any(e.path == "frontmatter.colour" and e.severity == "error" for e in errors)


def test_strict_frontmatter_warn():
    schema = {"additional_fields": "warn",
              "frontmatter": {"fields": {"id": {}}}, "sections": []}
    errors = SchemaValidator(schema).validate({"id": "x", "colour": "blue"}, None, [])
    assert any(e.path == "frontmatter.colour" and e.severity == "warning" for e in errors)


def test_frontmatter_permissive_by_default():
    schema = {"frontmatter": {"fields": {"id": {}}}, "sections": []}
    errors = SchemaValidator(schema).validate({"id": "x", "colour": "blue"}, None, [])
    assert not any("colour" in e.path for e in errors)


def test_strict_frontmatter_exempts_document_type():
    schema = {"additional_fields": False,
              "frontmatter": {"fields": {"id": {}}}, "sections": []}
    errors = SchemaValidator(schema).validate({"id": "x", "document_type": "thing"}, None, [])
    assert not any("document_type" in e.path for e in errors)


# ── strikethrough id-recovery honours the schema's id format ──
#
# A GFM strikethrough span crossing the arrow defeats the AST ref extraction,
# so visit_list_item falls back to a raw-text regex. That regex must use the
# id format the *schema* declares (not a hard-coded convention), so a non-kebab
# id isn't silently dropped — and an id that doesn't match the schema isn't
# spuriously grabbed.

def _first_ref(item_md, id_pattern=None):
    from marko.block import List as MdList
    from cartulary.validator import visit_list
    _, _, sections = parse("# T\n\n## Friends\n\n" + item_md + "\n")
    lst = next(n for n in sections[0].children if isinstance(n, MdList))
    return visit_list(lst, "→", id_pattern)[0].ref


def test_strikethrough_fallback_recovers_id_matching_schema_pattern():
    assert _first_ref("- ~Frodo → `frodo_baggins`~", r"[a-z]+_[a-z]+") == "frodo_baggins"


def test_strikethrough_fallback_rejects_id_not_matching_schema_pattern():
    # Schema declares snake_case ids; a kebab id must not be recovered.
    assert _first_ref("- ~Frodo → `frodo-baggins`~", r"[a-z]+_[a-z]+") is None


def test_strikethrough_fallback_generic_floor_without_pattern():
    # No declared pattern → permissive generic token (snake, kebab, single).
    assert _first_ref("- ~Frodo → `frodo_baggins`~") == "frodo_baggins"
    assert _first_ref("- ~Frodo → `frodo`~") == "frodo"


def test_id_pattern_for_derives_anchorless_pattern_from_local_pk():
    schema = {"value_types": {"snake_id": {"pattern": "^[a-z]+_[a-z]+$"}},
              "frontmatter": {"fields": {"pid": {"type": "snake_id", "primary_key": True}}},
              "sections": []}
    assert SchemaValidator(schema)._id_pattern_for("pid") == "[a-z]+_[a-z]+"


def test_snake_ids_reciprocate_through_strikethrough(tmp_path):
    schema = {
        "value_types": {"snake_id": {"pattern": "^[a-z]+_[a-z]+$"}},
        "schemas": {
            "person": {
                "primary_key": "pid",
                "frontmatter": {"fields": {
                    "document_type": {"value": "person", "required": True},
                    "pid": {"type": "snake_id", "required": True, "primary_key": True},
                }},
                "sections": [{"heading": "Friends",
                              "content": {"type": "ref_list", "style": "unlabeled",
                                          "ref": "person", "inverse": "Friends"}}],
            }
        },
    }
    sp = dump_schema(tmp_path, "s.yaml", schema)
    a = write(tmp_path, "a.md",
              "---\ndocument_type: person\npid: aa_one\n---\n\n# A\n\n## Friends\n\n- ~B → `bb_two`~\n")
    b = write(tmp_path, "b.md",
              "---\ndocument_type: person\npid: bb_two\n---\n\n# B\n\n## Friends\n\n- ~A → `aa_one`~\n")
    results = validate_files(sp, [a, b])
    flat = [f"[{e.path}] {e.message}" for errs in results.values() for e in errs]
    assert flat == [], flat


# ── blast-radius provenance & --changed scoping ──────────────
#
# Cross-document findings are attributed to a counterpart, not the file that
# caused them (a one-sided link edited into A is reported on B). caused_by
# records the files a finding depends on so scope_to_changed can bound output
# to what a set of changed files is responsible for.

def _kin_schema(dir_):
    return dump_schema(dir_, "s.yaml", {
        "value_types": {"pid": {"pattern": "^[a-z]+$"}},
        "schemas": {"person": {
            "primary_key": "person_id",
            "frontmatter": {"fields": {
                "document_type": {"value": "person", "required": True},
                "person_id": {"type": "pid", "required": True, "primary_key": True},
                "name": {"required": True},
            }},
            "sections": [
                {"heading": "Parents",
                 "content": {"type": "ref_list", "style": "unlabeled", "ref": "person", "inverse": "Children"}},
                {"heading": "Children",
                 "content": {"type": "ref_list", "style": "unlabeled", "ref": "person", "inverse": "Parents"}},
            ],
        }},
    })


def _person(dir_, pid, parents_ref=None):
    body = f"---\ndocument_type: person\nperson_id: {pid}\nname: {pid.title()}\n---\n\n# {pid}\n\n## Parents\n"
    if parents_ref:
        body += f"\n- Ref → `{parents_ref}`\n"
    body += "\n## Children\n"
    return write(dir_, f"{pid}.md", body)


def _reciprocity_finding(results, host_name):
    for fp, errs in results.items():
        if Path(fp).name == host_name:
            for e in errs:
                if "reciprocal" in e.message.lower():
                    return e
    return None


def test_reciprocity_finding_is_attributed_to_counterpart(tmp_path):
    # alice lists bob as a parent; bob does not reciprocate.
    sp = _kin_schema(tmp_path)
    alice = _person(tmp_path, "alice", parents_ref="bob")
    bob = _person(tmp_path, "bob")
    results = validate_files(sp, [alice, bob])
    # The finding lands on bob.md, not alice.md ...
    assert _reciprocity_finding(results, "alice.md") is None
    finding = _reciprocity_finding(results, "bob.md")
    assert finding is not None
    # ... but its blast radius names both files.
    assert finding.caused_by == {alice, bob}


def test_scope_to_changed_surfaces_counterpart_finding(tmp_path):
    sp = _kin_schema(tmp_path)
    alice = _person(tmp_path, "alice", parents_ref="bob")
    bob = _person(tmp_path, "bob")
    carol = _person(tmp_path, "carol")  # unrelated, clean-but-irrelevant
    results = validate_files(sp, [alice, bob, carol])

    # Editing alice must surface the finding reported on bob (alice caused it).
    scoped = scope_to_changed(results, [alice])
    assert bob in scoped and any("reciprocal" in e.message.lower() for e in scoped[bob])
    # A changed file that touches nothing yields no findings.
    assert scope_to_changed(results, [carol]) == {}


def test_scope_to_changed_excludes_unrelated_preexisting_findings(tmp_path):
    sp = _kin_schema(tmp_path)
    alice = _person(tmp_path, "alice", parents_ref="bob")
    bob = _person(tmp_path, "bob")
    # dave has an unrelated error (missing required `name`).
    dave = write(tmp_path, "dave.md",
                 "---\ndocument_type: person\nperson_id: dave\n---\n\n# dave\n\n## Parents\n\n## Children\n")
    results = validate_files(sp, [alice, bob, dave])

    scoped = scope_to_changed(results, [alice])
    # dave's pre-existing error is out of alice's scope.
    assert dave not in scoped
    # dave's own change is in scope for dave.
    assert dave in scope_to_changed(results, [dave])


def test_duplicate_key_caused_by_all_colliding_files(tmp_path):
    sp = _kin_schema(tmp_path)
    a = write(tmp_path, "a.md", "---\ndocument_type: person\nperson_id: dup\nname: A\n---\n\n# a\n\n## Parents\n\n## Children\n")
    b = write(tmp_path, "b.md", "---\ndocument_type: person\nperson_id: dup\nname: B\n---\n\n# b\n\n## Parents\n\n## Children\n")
    results = validate_files(sp, [a, b])
    dups = [e for errs in results.values() for e in errs if "Duplicate primary key" in e.message]
    assert dups, "expected a duplicate-key finding"
    assert all(e.caused_by == {a, b} for e in dups)
    # Editing either colliding file surfaces the duplicate.
    assert scope_to_changed(results, [a]) and scope_to_changed(results, [b])


def test_scope_to_changed_matches_relative_and_absolute_paths(tmp_path):
    sp = _kin_schema(tmp_path)
    alice = _person(tmp_path, "alice", parents_ref="bob")
    bob = _person(tmp_path, "bob")
    results = validate_files(sp, [alice, bob])
    # Pass the changed file in a non-normalized form; it must still match.
    messy = str(tmp_path / "." / "alice.md")
    assert bob in scope_to_changed(results, [messy])


# ── finding rule ids (taxonomy) & SARIF output ───────────────

def _by_path(results):
    return {e.path: e for errs in results.values() for e in errs}


def test_every_finding_carries_a_rule_id():
    results = validate_files(str(LIBRARY), [str(FIXTURES / "bad-book-2099.md")])
    findings = [e for errs in results.values() for e in errs]
    assert findings, "fixture should produce findings"
    assert all(e.rule for e in findings), \
        [f"{e.path}: {e.rule!r}" for e in findings if not e.rule]


def test_rule_ids_for_representative_findings():
    results = validate_files(str(LIBRARY), [str(FIXTURES / "bad-book-2099.md")])
    byp = _by_path(results)
    expected = {
        "title": "title-mismatch",
        "frontmatter.status": "enum-mismatch",
        "frontmatter.year": "type-mismatch",
        "sections": "section-order",
        "section[Written By].item": "unresolved-reference",
        "section[Editions].row[0].Year": "type-mismatch",
        "section[Editions].row[0].Format": "enum-mismatch",
        "section[Editions].row[0].ISBN": "type-mismatch",
    }
    for path, rule in expected.items():
        assert path in byp, f"missing finding at {path}"
        assert byp[path].rule == rule, f"{path}: got {byp[path].rule!r}, want {rule!r}"


def test_rule_missing_reciprocal(tmp_path):
    sp = _kin_schema(tmp_path)
    results = validate_files(sp, [_person(tmp_path, "alice", parents_ref="bob"),
                                  _person(tmp_path, "bob")])
    assert _reciprocity_finding(results, "bob.md").rule == "missing-reciprocal"


def test_rule_duplicate_key(tmp_path):
    sp = _kin_schema(tmp_path)
    a = write(tmp_path, "a.md", "---\ndocument_type: person\nperson_id: dup\nname: A\n---\n\n# a\n\n## Parents\n\n## Children\n")
    b = write(tmp_path, "b.md", "---\ndocument_type: person\nperson_id: dup\nname: B\n---\n\n# b\n\n## Parents\n\n## Children\n")
    results = validate_files(sp, [a, b])
    assert any(e.rule == "duplicate-key" for errs in results.values() for e in errs)


def test_rule_unknown_field():
    schema = {"additional_fields": False, "frontmatter": {"fields": {"id": {}}}, "sections": []}
    errors = SchemaValidator(schema).validate({"id": "x", "colour": "blue"}, None, [])
    assert any(e.path == "frontmatter.colour" and e.rule == "unknown-field" for e in errors)


def test_sarif_is_valid_2_1_0_and_declares_used_rules():
    import json
    from cartulary.validator import results_to_sarif
    results = validate_files(str(LIBRARY), [str(FIXTURES / "bad-book-2099.md")])
    doc = json.loads(results_to_sarif(results))
    assert doc["version"] == "2.1.0"
    assert "$schema" in doc
    run = doc["runs"][0]
    assert run["tool"]["driver"]["name"] == "cartulary"
    declared = {r["id"] for r in run["tool"]["driver"]["rules"]}
    used = {res["ruleId"] for res in run["results"]}
    assert used, "expected results"
    assert used <= declared, f"undeclared rules: {used - declared}"


def test_sarif_result_shape():
    import json
    from cartulary.validator import results_to_sarif
    results = validate_files(str(LIBRARY), [str(FIXTURES / "bad-book-2099.md")])
    run = json.loads(results_to_sarif(results))["runs"][0]
    r = next(x for x in run["results"] if x["ruleId"] == "unresolved-reference")
    assert r["level"] == "error"
    assert r["message"]["text"]
    loc = r["locations"][0]
    assert loc["physicalLocation"]["artifactLocation"]["uri"].endswith("bad-book-2099.md")
    # structural path preserved as a logical location
    assert any(l["fullyQualifiedName"] == "section[Written By].item"
               for l in loc["logicalLocations"])


def test_sarif_clean_corpus_has_no_results():
    import json
    from cartulary.validator import results_to_sarif
    files = [str(FIXTURES / n) for n in VALID_CORPUS]
    doc = json.loads(results_to_sarif(validate_files(str(LIBRARY), files)))
    assert doc["runs"][0]["results"] == []



# ── 0.2.0: schema is the contract (no silent under-validation) ──
#
# A malformed schema must not quietly under-validate. validate_files/validate_file
# treat schema validity as a hard precondition: an invalid schema raises
# SchemaError (you can't validate documents against a broken contract). Unknown /
# misplaced keys are errors (a typo means the intended rule never runs), with an
# `x-` escape hatch for intentional annotations. To *inspect* schema findings
# without raising, call validate_schema() directly (the CLI/editors do).

from cartulary import SchemaError  # noqa: E402


def _schema_with_unknown_key(dir_):
    # `filename_patttern` (typo) is unknown -> the author's rule never runs.
    return dump_schema(dir_, "bad.yaml", {
        "filename_patttern": "{slug}.md",
        "frontmatter": {"fields": {"slug": {"required": True}}},
        "sections": [],
    })


def test_validate_files_raises_on_invalid_schema(tmp_path):
    sp = _schema_with_unknown_key(tmp_path)
    doc = write(tmp_path, "doc.md", "---\nslug: doc\n---\n\n# X\n")
    with pytest.raises(SchemaError) as exc:
        validate_files(sp, [doc])
    # the raised error carries the findings for inspection
    assert any("filename_patttern" in f.message for f in exc.value.findings)


def test_validate_file_raises_on_invalid_schema(tmp_path):
    sp = _schema_with_unknown_key(tmp_path)
    doc = write(tmp_path, "doc.md", "---\nslug: doc\n---\n\n# X\n")
    with pytest.raises(SchemaError):
        validate_file(sp, doc)


def test_validate_schema_inspects_without_raising(tmp_path):
    # The dedicated schema-checking entry point returns findings, never raises.
    findings = validate_schema(_schema_with_unknown_key(tmp_path))
    assert any(f.severity == "error" and "filename_patttern" in f.message for f in findings)


def test_clean_schema_validates(tmp_path):
    sp = dump_schema(tmp_path, "ok.yaml",
                     {"frontmatter": {"fields": {"id": {"required": True}}}, "sections": []})
    doc = write(tmp_path, "doc.md", "---\nid: doc\n---\n\n# X\n")
    results = validate_files(sp, [doc])
    assert all(not errs for errs in results.values())


def test_x_prefixed_schema_keys_are_allowed():
    schema = {"x-note": "internal", "frontmatter": {"fields": {"id": {"x-ann": 1}}}, "sections": []}
    findings = validate_schema(schema)
    assert not any("x-note" in f.message or "x-ann" in f.message for f in findings)


# ── 0.2.0: general filename_pattern template ──

def _fnpat_schema(dir_):
    return dump_schema(dir_, "fn.yaml", {
        "filename_pattern": "{slug}.md",
        "frontmatter": {"fields": {"slug": {"required": True}}},
        "sections": [],
    })


def test_filename_pattern_is_a_supported_key(tmp_path):
    findings = validate_schema(_fnpat_schema(tmp_path))
    assert not any("filename_pattern" in f.message for f in findings)  # not "unknown key"


def test_filename_pattern_accepts_matching_name(tmp_path):
    sp = _fnpat_schema(tmp_path)
    doc = write(tmp_path, "aragorn.md", "---\nslug: aragorn\n---\n\n# A\n")
    errors = validate_file(sp, doc)
    assert not any(e.path == "filename" for e in errors), [e.message for e in errors]


def test_filename_pattern_rejects_wrong_name(tmp_path):
    sp = _fnpat_schema(tmp_path)
    doc = write(tmp_path, "WRONG.md", "---\nslug: aragorn\n---\n\n# A\n")
    errors = validate_file(sp, doc)
    assert any(e.path == "filename" and e.severity == "error" for e in errors)


def test_filename_pattern_meta_check_flags_unknown_field(tmp_path):
    schema = {"filename_pattern": "{nope}.md", "frontmatter": {"fields": {"slug": {}}}, "sections": []}
    findings = validate_schema(schema)
    assert any(f.severity == "error" and "filename_pattern" in f.path for f in findings)
