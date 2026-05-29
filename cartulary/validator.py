#!/usr/bin/env python3
"""
cartulary: Validate a corpus of Markdown documents against a YAML schema.

Architecture:
  1. marko (GFM) lexes+parses markdown into a typed AST
  2. SectionTreeBuilder walks the flat AST and groups nodes under
     their heading into a section tree (the step marko doesn't do)
  3. SchemaValidator walks the section tree and validates against
     the YAML schema definition, dispatching by content type

This mirrors the classic compiler pipeline:
  lex → parse → AST → semantic analysis
  (marko)  (marko)  (SectionTreeBuilder)  (SchemaValidator)
"""

import json
import re
import sys
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

import marko
from marko import Markdown
from marko.ext.gfm import GFM
from marko.block import Heading, List, ListItem, Paragraph, BlankLine, Document as MarkoDocument
from marko.inline import StrongEmphasis, RawText, CodeSpan
from marko.ext.gfm.elements import Table, TableRow, TableCell


# ════════════════════════════════════════════════════════════
# Layer 1: Frontmatter extraction
# ════════════════════════════════════════════════════════════

class FrontmatterError(ValueError):
    """Raised when a file's YAML frontmatter is present but does not parse."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter from markdown text.

    Raises :class:`FrontmatterError` if the frontmatter block is present but
    not valid YAML, so callers can report it instead of silently treating the
    document as having no frontmatter.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end].strip()
    body = text[end + 4:].strip()
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise FrontmatterError(str(e).replace("\n", " ")) from e
    # Normalize values to strings for uniform validation (preserve lists)
    for k, v in fm.items():
        if v is not None and not isinstance(v, (str, list)):
            fm[k] = str(v)
    return fm, body


# ════════════════════════════════════════════════════════════
# Layer 2: Section tree builder
#
# marko's AST is flat — headings are siblings of their content.
# Markdown *implies* a tree (content belongs to the heading above
# it) but no parser builds that tree for us. This is the "yacc"
# step: we take the token stream and impose structure.
# ════════════════════════════════════════════════════════════

@dataclass
class Section:
    """A heading and all AST nodes that belong to it."""
    heading: str
    level: int
    children: list  # marko AST nodes (Table, List, Paragraph, etc.)
    subsections: list["Section"] = field(default_factory=list)


def build_section_tree(doc: MarkoDocument) -> tuple[str | None, list[Section]]:
    """
    Walk the flat AST and group nodes under headings.
    Returns (title, top_level_sections).

    Uses a stack to nest deeper headings under their parent.
    An H3 becomes a subsection of the preceding H2, an H4 nests
    under the preceding H3, etc.
    """
    title = None
    sections: list[Section] = []  # top-level (H2) sections
    # Stack of (section, level) — tracks nesting context.
    # stack[-1] is always the most recent section at any level.
    stack: list[Section] = []

    for node in doc.children:
        if isinstance(node, Heading):
            heading_text = extract_text(node).strip()
            if node.level == 1 and title is None:
                title = heading_text
                continue
            new_section = Section(
                heading=heading_text,
                level=node.level,
                children=[],
            )
            # Pop back to the parent level: any section on the stack
            # at the same level or deeper is no longer the current context.
            while stack and stack[-1].level >= node.level:
                stack.pop()

            if stack:
                # Nest under the parent section
                stack[-1].subsections.append(new_section)
            else:
                # Top-level section
                sections.append(new_section)

            stack.append(new_section)
        elif isinstance(node, BlankLine):
            continue
        elif stack:
            stack[-1].children.append(node)

    return title, sections


def extract_text(node) -> str:
    """Recursively extract plain text from any AST node."""
    if isinstance(getattr(node, "children", None), str):
        return node.children
    if isinstance(getattr(node, "children", None), list):
        return "".join(extract_text(c) for c in node.children)
    return ""


# ════════════════════════════════════════════════════════════
# Layer 3: AST node visitors
#
# Extract structured data from marko AST nodes. Each visitor
# knows how to read one kind of markdown construct and return
# domain objects that the validator can check.
# ════════════════════════════════════════════════════════════

@dataclass
class TableData:
    columns: list[str]
    rows: list[dict[str, str]]   # list of {column_name: cell_value}


@dataclass
class RefItem:
    raw: str
    label: str | None = None
    name: str | None = None
    ref: str | None = None


def visit_table(node: Table) -> TableData:
    """Extract column names and row data from a marko Table node."""
    columns = []
    rows = []
    for row_node in node.children:
        cells = [extract_text(cell).strip() for cell in row_node.children]
        if row_node.children and getattr(row_node.children[0], "header", False):
            columns = cells
        else:
            row_dict = {}
            for i, col in enumerate(columns):
                row_dict[col] = cells[i] if i < len(cells) else ""
            rows.append(row_dict)
    return TableData(columns=columns, rows=rows)


def visit_list_item(node: ListItem) -> RefItem:
    """Extract label, name, and cross-reference from a list item."""
    # Walk the inline children of the paragraph inside the list item
    item = RefItem(raw=extract_text(node).strip())

    for child in node.children:
        if not isinstance(child, Paragraph):
            continue
        parts = child.children if isinstance(child.children, list) else []
        for i, part in enumerate(parts):
            if isinstance(part, StrongEmphasis):
                label_text = extract_text(part).strip()
                if label_text.endswith(":"):
                    item.label = label_text[:-1]
            elif isinstance(part, CodeSpan):
                # CodeSpan after → is a cross-reference
                preceding_text = extract_text(parts[i - 1]) if i > 0 else ""
                if "→" in preceding_text:
                    item.ref = part.children.strip() if isinstance(part.children, str) else extract_text(part).strip()

    # Fallback: GFM strikethrough can swallow → `ref` when tildes are nearby,
    # and extract_text strips backticks.  Try with and without backticks.
    if item.ref is None:
        ref_m = re.search(r"→\s*`?([a-z0-9]+(?:-[a-z0-9]+)+)`?", item.raw)
        if ref_m:
            item.ref = ref_m.group(1).strip()

    # Extract name: everything that isn't the label or the ref
    name_text = item.raw
    if item.label:
        name_text = re.sub(r"\*\*\w[\w\s]*?:\*\*\s*", "", name_text)
    if item.ref:
        name_text = re.sub(r"\s*→\s*`[^`]+`", "", name_text)
    item.name = name_text.strip() or None

    return item


def visit_list(node: List) -> list[RefItem]:
    """Extract all items from a bullet/ordered list."""
    return [visit_list_item(item) for item in node.children if isinstance(item, ListItem)]


# ════════════════════════════════════════════════════════════
# Layer 4: Schema-driven validator
#
# Walks the section tree and dispatches validation by the
# content type declared in the schema. Collects errors with
# paths for reporting.
# ════════════════════════════════════════════════════════════

@dataclass
class ValidationError:
    path: str
    message: str
    severity: str = "error"


class SchemaValidator:
    """Validate a parsed markdown document against a YAML schema."""

    def __init__(self, schema: dict, known_ids: set[str] | None = None,
                 pk_types: dict[str, dict] | None = None,
                 known_id_types: dict[str, str] | None = None,
                 pk_field_owners: dict[str, set[str]] | None = None):
        self.schema = schema
        self.value_types = schema.get("value_types", {})
        self.known_ids = known_ids
        self.pk_types = pk_types or {}  # ref_target_name -> field type defn
        # Cross-document type awareness: which document type owns each known id,
        # and which document type(s) each PK field name belongs to. Together
        # these let a ref be checked for pointing at the right *kind* of document.
        self.known_id_types = known_id_types or {}
        self.pk_field_owners = pk_field_owners or {}
        self.errors: list[ValidationError] = []
        self.refs_found: list[tuple[str, str, str | None]] = []  # (path, ref_value, ref_target)
        self.inverse_refs: list[tuple[str, str, str]] = []  # (section, target_id, inverse_section)
        self._pk_field, self._pk_defn = self._resolve_pk()

    def _resolve_pk(self) -> tuple[str | None, dict]:
        """Find the primary key field and its type definition.

        Checks for `primary_key: true` on frontmatter fields first,
        falls back to the document-level `primary_key` key.
        """
        fm_fields = self.schema.get("frontmatter", {}).get("fields", {})
        # Preferred: primary_key: true on the field itself
        for name, defn in fm_fields.items():
            if defn.get("primary_key"):
                return name, defn
        # Fallback: document-level primary_key pointing to a field name
        pk_name = self.schema.get("primary_key")
        if pk_name and pk_name in fm_fields:
            return pk_name, fm_fields[pk_name]
        return None, {}

    def validate(self, frontmatter: dict, title: str | None,
                 sections: list[Section], filepath: str | None = None) -> list[ValidationError]:
        self.errors = []
        self.refs_found = []
        self.inverse_refs = []
        self.filepath = filepath

        match_field = self.schema.get("filename_must_match")
        if filepath and match_field:
            stem = Path(filepath).stem
            field_val = frontmatter.get(match_field, "")
            if stem != field_val:
                self._error("filename", f"Filename '{stem}' does not match {match_field} '{field_val}'")

        self._check_frontmatter(frontmatter)
        self._check_title(title, frontmatter)
        self._check_sections(sections)
        self._check_refs()
        return self.errors

    def _error(self, path: str, message: str, severity: str = "error"):
        self.errors.append(ValidationError(path=path, message=message, severity=severity))

    # ── Cross-document ref checking ──────────────────────

    def _collect_ref(self, ref: str, path: str, ref_target: str | None = None):
        """Record a ref for cross-document resolution checking.

        If *ref_target* names a known PK field (via pk_types), validate
        the ref value against that PK's type definition.
        """
        self.refs_found.append((path, ref, ref_target))
        if ref_target and ref_target in self.pk_types:
            self._check_value(ref, self.pk_types[ref_target], path)

    def _check_refs(self):
        """If known_ids was provided, check that refs resolve — and, when the
        target's owning document type is known, that they resolve to the right
        *kind* of document (a `ref: author_id` must point at an author, not just
        at any id that happens to share the format)."""
        if self.known_ids is None:
            return
        for path, ref, ref_target in self.refs_found:
            if ref not in self.known_ids:
                self._error(path, f"Unresolved reference: '{ref}'")
                continue
            expected = self.pk_field_owners.get(ref_target) if ref_target else None
            actual = self.known_id_types.get(ref)
            if expected and actual and actual not in expected:
                want = ", ".join(sorted(expected))
                self._error(path, f"Reference '{ref}' resolves to a "
                                  f"'{actual}' document, but a '{want}' was expected")

    # ── Frontmatter ──────────────────────────────────────

    def _check_frontmatter(self, fm: dict):
        fields = self.schema.get("frontmatter", {}).get("fields", {})
        for name, defn in fields.items():
            value = fm.get(name)
            if defn.get("required") and (value is None or value == ""):
                self._error(f"frontmatter.{name}", f"Required field '{name}' is missing")
                continue
            if value is not None:
                # Handle list-valued fields (e.g. persons)
                if isinstance(value, list):
                    items_def = defn.get("items")
                    for i, item in enumerate(value):
                        if item is None:
                            continue
                        path = f"frontmatter.{name}[{i}]"
                        if items_def is not None:
                            self._check_list_item(item, items_def, path)
                        else:
                            # Legacy scalar-only path: stringify and validate
                            self._check_value(str(item), defn, path)
                            if defn.get("ref"):
                                self._collect_ref(str(item), path, ref_target=defn["ref"])
                else:
                    self._check_value(str(value), defn, f"frontmatter.{name}")
                    # Collect refs for cross-document validation
                    if defn.get("ref") and value:
                        self._collect_ref(str(value), f"frontmatter.{name}",
                                          ref_target=defn["ref"])

    # ── List-item validation (items: { ... }) ────────────

    def _check_list_item(self, item, items_def: dict, path: str):
        """Validate one list item against an `items:` spec.

        items_def shapes:
          { type: object, fields: {...} }  — dict item with named sub-fields
          { any_of: [option1, option2] }   — first matching option wins
          { type: <scalar_type>, ... }     — scalar item (legacy form)
        """
        if "any_of" in items_def:
            for opt in items_def["any_of"]:
                if self._option_matches(item, opt):
                    self._validate_against_option(item, opt, path)
                    return
            self._error(path, f"Item does not match any_of options")
            return

        if items_def.get("type") == "object":
            self._validate_object_item(item, items_def, path)
            return

        # Scalar items spec: same as the legacy field-level scalar handling
        self._check_value(str(item), items_def, path)
        if items_def.get("ref"):
            self._collect_ref(str(item), path, ref_target=items_def["ref"])

    def _validate_against_option(self, item, opt: dict, path: str):
        """Run real validation (writes errors, collects refs) against a chosen any_of option."""
        if opt.get("type") == "object":
            self._validate_object_item(item, opt, path)
        else:
            self._check_value(str(item), opt, path)
            if opt.get("ref"):
                self._collect_ref(str(item), path, ref_target=opt["ref"])

    def _validate_object_item(self, item, item_def: dict, path: str):
        """Validate a dict item against an object spec with named sub-fields."""
        if not isinstance(item, dict):
            self._error(path, f"Expected object (dict), got {type(item).__name__}")
            return
        sub_fields = item_def.get("fields", {})
        for fname, fdef in sub_fields.items():
            fval = item.get(fname)
            sub_path = f"{path}.{fname}"
            if fdef.get("required") and (fval is None or fval == ""):
                self._error(sub_path, f"Required field '{fname}' is missing")
                continue
            if fval is None:
                continue
            self._check_value(str(fval), fdef, sub_path)
            if fdef.get("ref"):
                self._collect_ref(str(fval), sub_path, ref_target=fdef["ref"])

    def _option_matches(self, item, opt: dict) -> bool:
        """Pure shape check — does item match this option? No errors written, no refs collected."""
        if opt.get("type") == "object":
            if not isinstance(item, dict):
                return False
            sub_fields = opt.get("fields", {})
            for fname, fdef in sub_fields.items():
                fval = item.get(fname)
                if fdef.get("required") and (fval is None or fval == ""):
                    return False
                if fval is not None and not self._value_passes(str(fval), fdef):
                    return False
            return True
        # Scalar option
        if isinstance(item, dict):
            return False
        return self._value_passes(str(item), opt)

    def _value_passes(self, value: str, defn: dict) -> bool:
        """Pure: does value pass type/value/enum check? Mirrors _check_value without writing errors."""
        if "value" in defn:
            return value == str(defn["value"])
        if "enum" in defn:
            return value in [str(v) for v in defn["enum"]]
        type_name = defn.get("type")
        if not type_name or type_name == "string":
            return True
        type_def = self.value_types.get(type_name)
        if not type_def:
            return True
        if "pattern" in type_def:
            return bool(re.match(type_def["pattern"], value))
        if "enum" in type_def:
            return value in [str(v) for v in type_def["enum"]]
        if "any_of" in type_def:
            return self._matches_any_of(value, type_def["any_of"])
        return True

    # ── Title ────────────────────────────────────────────

    def _check_title(self, title: str | None, fm: dict):
        pattern = self.schema.get("title_pattern")
        if not pattern:
            return
        if title is None:
            self._error("title", "Missing H1 title")
            return
        # Build expected title by substituting {field} placeholders.
        # Also build a regex that allows optional '~' before date-like values
        # (e.g. birth/death years) since '~' is a common "circa" convention.
        expected = pattern
        regex_parts = []
        last_end = 0
        for match in re.finditer(r"\{(\w+)\}", pattern):
            field_name = match.group(1)
            value = str(fm.get(field_name, ""))
            expected = expected.replace(match.group(0), value, 1)
            # Build regex: literal text before this placeholder + optional ~ + value
            regex_parts.append(re.escape(pattern[last_end:match.start()]))
            regex_parts.append(f"~?{re.escape(value)}")
            last_end = match.end()
        regex_parts.append(re.escape(pattern[last_end:]))
        title_re = "^" + "".join(regex_parts) + "$"
        if not re.match(title_re, title):
            self._error("title", f"Title '{title}' does not match expected '{expected}'")

    # ── Value checking ───────────────────────────────────

    def _check_value(self, value: str, defn: dict, path: str):
        if "value" in defn:
            if value != str(defn["value"]):
                self._error(path, f"'{value}' must be '{defn['value']}'")
            return
        if "enum" in defn:
            if value not in [str(v) for v in defn["enum"]]:
                self._error(path, f"'{value}' not in {defn['enum']}")
            return

        type_name = defn.get("type")
        if not type_name or type_name == "string":
            return

        type_def = self.value_types.get(type_name)
        if not type_def:
            return

        if "pattern" in type_def:
            if not re.match(type_def["pattern"], value):
                desc = type_def.get("description", type_def["pattern"])
                self._error(path, f"'{value}' doesn't match type '{type_name}' ({desc})")
                return  # skip exists check if pattern fails
        elif "enum" in type_def:
            if value not in [str(v) for v in type_def["enum"]]:
                self._error(path, f"'{value}' not in {type_name} values {type_def['enum']}")
            return
        elif "any_of" in type_def:
            if not self._matches_any_of(value, type_def["any_of"]):
                self._error(path, f"'{value}' doesn't match any option for type '{type_name}'")
            return

        # File existence check: resolve value relative to the validated file
        exists_def = type_def.get("exists")
        if exists_def and self.filepath:
            roots = exists_def.get("relative_to", ".")
            if isinstance(roots, str):
                roots = [roots]
            file_dir = Path(self.filepath).parent
            found = any(
                (file_dir / rel / value).resolve().exists()
                for rel in roots
            )
            if not found:
                severity = exists_def.get("severity", "warning")
                self._error(path, f"File not found: {value}", severity=severity)

    def _matches_any_of(self, value: str, options: list[dict]) -> bool:
        for opt in options:
            if "literal" in opt and value == opt["literal"]:
                return True
            if "type" in opt:
                td = self.value_types.get(opt["type"])
                if td and "pattern" in td and re.match(td["pattern"], value):
                    return True
        return False

    # ── Section structure ────────────────────────────────

    def _check_sections(self, doc_sections: list[Section]):
        schema_sections = self.schema.get("sections", [])
        doc_headings = [s.heading for s in doc_sections]
        schema_order = [s["heading"] for s in schema_sections]

        # Required sections
        for ss in schema_sections:
            req = ss.get("required")
            if req and ss["heading"] not in doc_headings:
                severity = "warning" if req == "warn" else "error"
                self._error("sections", f"Required section '{ss['heading']}' is missing", severity=severity)

        # Deprecated sections
        for ss in schema_sections:
            if ss.get("deprecated") and ss["heading"] in doc_headings:
                self._error(f"section[{ss['heading']}]", "Deprecated section — should be removed", severity="warning")

        # Ordering
        doc_known = [h for h in doc_headings if h in schema_order]
        expected = [h for h in schema_order if h in doc_known]
        if doc_known != expected:
            self._error("sections", f"Section order: got {doc_known}, expected {expected}")

        # Position: last
        for ss in schema_sections:
            if ss.get("position") == "last" and ss["heading"] in doc_headings:
                if doc_headings[-1] != ss["heading"]:
                    self._error(f"section[{ss['heading']}]", f"Must be the last section")

        # Unknown sections
        additional = self.schema.get("additional_sections", False)
        schema_set = set(schema_order)
        for heading in doc_headings:
            if heading not in schema_set:
                if additional == "warn":
                    self._error("sections", f"Unknown section '{heading}'", severity="warning")
                elif not additional:
                    self._error("sections", f"Unknown section '{heading}'")

        # Content and subsection validation per section
        for section in doc_sections:
            ss = next((s for s in schema_sections if s["heading"] == section.heading), None)
            if ss:
                if ss.get("content"):
                    self._check_content(section, ss["content"])
                if section.subsections or ss.get("subsections"):
                    self._check_subsections(section, ss)

    def _check_subsections(self, section: Section, schema_section: dict):
        """Validate subsections (H3+ under an H2, etc.)."""
        path = f"section[{section.heading}]"
        sub_schemas = schema_section.get("subsections", [])
        sub_headings = [s.heading for s in section.subsections]
        schema_sub_order = [s["heading"] for s in sub_schemas]

        # Required subsections
        for ss in sub_schemas:
            req = ss.get("required")
            if req and ss["heading"] not in sub_headings:
                severity = "warning" if req == "warn" else "error"
                self._error(path, f"Required subsection '{ss['heading']}' is missing", severity=severity)

        # Deprecated subsections
        for ss in sub_schemas:
            if ss.get("deprecated") and ss["heading"] in sub_headings:
                self._error(path, f"Deprecated subsection '{ss['heading']}' — should be removed", severity="warning")

        # Ordering (among known subsections)
        doc_known = [h for h in sub_headings if h in schema_sub_order]
        expected = [h for h in schema_sub_order if h in doc_known]
        if doc_known != expected:
            self._error(path, f"Subsection order: got {doc_known}, expected {expected}")

        # Unknown subsections: per-section override, then document-level, default false.
        # Prose sections without explicit subsection schemas default to allowing them.
        content_type = schema_section.get("content", {}).get("type") if schema_section.get("content") else None
        prose_default = True if (content_type == "prose" and not sub_schemas) else False
        additional = schema_section.get(
            "additional_subsections",
            self.schema.get("additional_subsections", prose_default),
        )
        schema_sub_set = set(schema_sub_order)
        for heading in sub_headings:
            if heading not in schema_sub_set:
                if additional == "warn":
                    self._error(path, f"Unknown subsection '{heading}'", severity="warning")
                elif not additional:
                    self._error(path, f"Unknown subsection '{heading}'")

        # Content validation per subsection
        for sub in section.subsections:
            ss = next((s for s in sub_schemas if s["heading"] == sub.heading), None)
            if ss:
                if ss.get("content"):
                    self._check_content(sub, ss["content"])
                # Recurse for deeper nesting (H4 under H3, etc.)
                if sub.subsections or ss.get("subsections"):
                    self._check_subsections(sub, ss)

    # ── Content dispatch (the visitor pattern) ───────────

    def _check_content(self, section: Section, content_def: dict):
        """Dispatch to the appropriate content checker by type."""
        content_type = content_def.get("type")
        path = f"section[{section.heading}]"

        if content_type == "table":
            self._check_table(section, content_def, path)
        elif content_type == "ref_list":
            self._check_ref_list(section, content_def, path)
        elif content_type == "log":
            self._check_log(section, content_def, path)

    # ── Table content ────────────────────────────────────

    def _check_table(self, section: Section, defn: dict, path: str):
        tables = [n for n in section.children if isinstance(n, Table)]
        if not tables:
            if defn.get("min_rows", 0) > 0:
                self._error(path, f"Expected a table with at least {defn['min_rows']} row(s)")
            return

        expected_cols = list(defn.get("columns", {}).keys())
        min_rows = defn.get("min_rows", 0)
        col_defs = defn.get("columns", {})
        total_rows = 0

        for table_idx, table_node in enumerate(tables):
            td = visit_table(table_node)
            tpath = f"{path}.table[{table_idx}]" if len(tables) > 1 else path

            if td.columns != expected_cols:
                self._error(tpath, f"Columns {td.columns} don't match expected {expected_cols}")

            total_rows += len(td.rows)

            for row_idx, row in enumerate(td.rows):
                for col_name, col_def in col_defs.items():
                    value = row.get(col_name, "")
                    cell_path = f"{tpath}.row[{row_idx}].{col_name}"
                    nullable = col_def.get("nullable", False)
                    if not value or value in ("—", "-", "–"):
                        if nullable == "warn":
                            self._error(cell_path, "Empty but not nullable", severity="warning")
                        elif not nullable:
                            self._error(cell_path, "Empty but not nullable")
                        continue
                    self._check_value(value, col_def, cell_path)
                    if col_def.get("ref"):
                        self._collect_ref(value, cell_path,
                                          ref_target=col_def["ref"])

        if total_rows < min_rows:
            self._error(path, f"Table(s) have {total_rows} row(s), need at least {min_rows}")

    # ── Reference list content ───────────────────────────

    def _check_ref_list(self, section: Section, defn: dict, path: str):
        lists = [n for n in section.children if isinstance(n, List)]
        if not lists:
            if defn.get("style") == "labeled":
                self._error(path, "Expected a reference list but none found")
            elif defn.get("min_items", 0) > 0:
                self._error(path, f"Expected at least {defn['min_items']} item(s)")
            return

        items: list[RefItem] = []
        for lst in lists:
            items.extend(visit_list(lst))
        style = defn.get("style", "unlabeled")
        ref_target = defn.get("ref")

        if style == "labeled":
            for expected in defn.get("items", []):
                label = expected["label"]
                matching = [it for it in items if it.label == label]
                if not matching:
                    self._error(f"{path}.{label}", f"Missing required item '{label}'")
                    continue
                item = matching[0]
                if item.ref is not None:
                    self._collect_ref(item.ref, f"{path}.{label}",
                                      ref_target=expected.get("ref", ref_target))
                    inverse = expected.get("inverse")
                    if inverse:
                        self.inverse_refs.append((section.heading, item.ref, inverse))
                elif not expected.get("allow_unknown"):
                    unknown_literals = ["Unknown", "unknown"]
                    if item.name not in unknown_literals:
                        self._error(f"{path}.{label}",
                                    f"No cross-reference and not marked Unknown",
                                    severity="warning")

        elif style == "unlabeled":
            unknown_literals = ["Unknown", "unknown", "None known"]
            real = [it for it in items if it.raw.strip() not in unknown_literals]
            min_items = defn.get("min_items", 0)
            if len(real) < min_items:
                self._error(path, f"Need at least {min_items} item(s), found {len(real)}")
            inverse = defn.get("inverse")
            for item in real:
                if item.ref is not None:
                    self._collect_ref(item.ref, f"{path}.item",
                                      ref_target=ref_target)
                    if inverse:
                        self.inverse_refs.append((section.heading, item.ref, inverse))

    # ── Log content ──────────────────────────────────────

    def _check_log(self, section: Section, defn: dict, path: str):
        pattern = defn.get("entry_pattern")
        if not pattern:
            return
        lists = [n for n in section.children if isinstance(n, List)]
        if not lists:
            self._error(path, "Expected a log list but none found")
            return
        items = visit_list(lists[0])
        for idx, item in enumerate(items):
            if not re.match(pattern, item.raw):
                self._error(f"{path}.entry[{idx}]",
                            f"Doesn't match log format: '{item.raw}'")


# ════════════════════════════════════════════════════════════
# Pipeline: tie it all together
# ════════════════════════════════════════════════════════════

def _extract_raw_title(body: str) -> str | None:
    """Extract H1 title from raw markdown before AST parsing.

    marko's GFM extension treats ~ as strikethrough, mangling titles
    like '# Name (~1842–~1901)'. We pull the title from raw text instead.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    return None


def _parse_file(filepath: str) -> tuple[dict[str, Any], str | None, list[Section]]:
    """Parse a markdown file into (frontmatter, title, sections)."""
    text = Path(filepath).read_text()
    frontmatter, body = split_frontmatter(text)
    raw_title = _extract_raw_title(body)
    md = Markdown(extensions=[GFM])
    ast = md.parse(body)
    _, sections = build_section_tree(ast)
    return frontmatter, raw_title, sections


def validate_file(schema_path: str, filepath: str) -> list[ValidationError]:
    """Validate a single file (no cross-document ref checking)."""
    loaded = load_schema(schema_path)
    try:
        frontmatter, title, sections = _parse_file(filepath)
    except FrontmatterError as e:
        return [ValidationError(path="frontmatter",
                                message=f"Invalid YAML frontmatter: {e.detail}")]
    if loaded.get("_multi"):
        dt = frontmatter.get("document_type")
        schema = loaded["schemas"].get(str(dt)) if dt else None
        if schema is None:
            return [ValidationError(
                path="frontmatter.document_type",
                message=f"Unknown or missing document_type '{dt}'",
            )]
    else:
        schema = loaded
    validator = SchemaValidator(schema)
    return validator.validate(frontmatter, title, sections, filepath=filepath)


def _resolve_pk_field(schema: dict) -> str | None:
    """Find the primary key field name for a schema."""
    fm_fields = schema.get("frontmatter", {}).get("fields", {})
    for name, defn in fm_fields.items():
        if defn.get("primary_key"):
            return name
    return schema.get("primary_key")


def validate_files(schema_path: str, filepaths: list[str]) -> dict[str, list[ValidationError]]:
    """
    Validate multiple files with cross-document reference checking.

    Supports both single-schema and multi-schema files.  In multi-schema
    mode, each file's ``document_type`` frontmatter field selects the
    schema to validate against.  Primary keys from all schemas share a
    single namespace so cross-document refs resolve across document types.

    Pass 1: parse all files, collect primary keys.
    Pass 2: validate each file with known_ids so unresolved refs
            are reported as warnings.
    Pass 3: check inverse ref reciprocity.
    """
    loaded = load_schema(schema_path)
    multi = loaded.get("_multi", False)
    if multi:
        schemas = loaded["schemas"]
    else:
        # Single schema: wrap as multi with a synthetic key
        doc_type = loaded.get("document", "_default")
        schemas = {doc_type: loaded}

    # Build pk_field per schema, and pk_types mapping ref target names to
    # their type definitions (so refs can be format-validated against the
    # correct PK type regardless of which schema the ref originates from).
    pk_fields: dict[str, str | None] = {}
    pk_types: dict[str, dict] = {}
    pk_field_owners: dict[str, set[str]] = {}  # pk field name -> document types using it
    for doc_type, schema in schemas.items():
        pk_name = _resolve_pk_field(schema)
        pk_fields[doc_type] = pk_name
        if pk_name:
            pk_field_owners.setdefault(pk_name, set()).add(doc_type)
            fm_fields = schema.get("frontmatter", {}).get("fields", {})
            if pk_name in fm_fields:
                pk_types[pk_name] = fm_fields[pk_name]

    # Pass 1: parse all files, route to schema, collect primary keys
    parsed: list[tuple[str, str | None, dict, dict, str | None, list[Section]]] = []
    # Each entry: (filepath, doc_type, schema, frontmatter, title, sections)
    known_ids: set[str] = set()
    id_to_file: dict[str, str] = {}
    id_to_type: dict[str, str] = {}
    duplicate_ids: dict[str, list[str]] = {}
    results: dict[str, list[ValidationError]] = {}

    for fp in filepaths:
        try:
            frontmatter, title, sections = _parse_file(fp)
        except FrontmatterError as e:
            results[fp] = [ValidationError(
                path="frontmatter",
                message=f"Invalid YAML frontmatter: {e.detail}",
            )]
            continue

        # Route to schema
        if multi:
            dt = frontmatter.get("document_type")
            if dt is None:
                results[fp] = [ValidationError(
                    path="frontmatter.document_type",
                    message="Missing 'document_type' field (required for multi-schema validation)",
                )]
                continue
            dt = str(dt)
            schema = schemas.get(dt)
            if schema is None:
                results[fp] = [ValidationError(
                    path="frontmatter.document_type",
                    message=f"Unknown document_type '{dt}' (expected one of: {', '.join(schemas.keys())})",
                )]
                continue
        else:
            dt = list(schemas.keys())[0]
            schema = schemas[dt]

        parsed.append((fp, dt, schema, frontmatter, title, sections))

        pk_field = pk_fields.get(dt)
        if pk_field:
            pk = frontmatter.get(pk_field)
            if pk:
                pk = str(pk)
                if pk in known_ids:
                    duplicate_ids.setdefault(pk, [id_to_file[pk]]).append(fp)
                else:
                    known_ids.add(pk)
                    id_to_file[pk] = fp
                    id_to_type[pk] = dt

    # Pass 2: validate each file, collect inverse refs
    inverse_index: dict[str, list[tuple[str, str, str]]] = {}
    ref_index: dict[str, set[tuple[str, str]]] = {}

    for fp, dt, schema, frontmatter, title, sections in parsed:
        validator = SchemaValidator(schema, known_ids=known_ids, pk_types=pk_types,
                                    known_id_types=id_to_type,
                                    pk_field_owners=pk_field_owners)
        errors = validator.validate(frontmatter, title, sections, filepath=fp)

        pk_field = pk_fields.get(dt)
        if pk_field:
            pk = frontmatter.get(pk_field)
            if pk:
                pk = str(pk)
                if pk in duplicate_ids:
                    others = [f for f in duplicate_ids[pk] if f != fp]
                    errors.append(ValidationError(
                        path=f"frontmatter.{pk_field}",
                        message=f"Duplicate primary key '{pk}' (also in: {', '.join(others)})",
                    ))

                ref_index[pk] = set()
                for section_heading, target_id, inverse_section in validator.inverse_refs:
                    inverse_index.setdefault(target_id, []).append(
                        (pk, section_heading, inverse_section)
                    )
                for _path, ref, _target in validator.refs_found:
                    if _path.startswith("section["):
                        sec = _path.split("]")[0].removeprefix("section[")
                        ref_index[pk].add((sec, ref))
                    elif _path.startswith("frontmatter."):
                        # Track frontmatter refs for cross-document resolution
                        ref_index[pk].add(("_frontmatter", ref))

        results[fp] = errors

    # Pass 3: check inverse ref reciprocity
    for target_id, expectations in inverse_index.items():
        if target_id not in ref_index:
            continue
        target_refs = ref_index[target_id]
        target_fp = id_to_file.get(target_id)
        if not target_fp:
            continue
        for source_id, source_section, inverse_section in expectations:
            if (inverse_section, source_id) not in target_refs:
                results[target_fp].append(ValidationError(
                    path=f"section[{inverse_section}]",
                    message=(
                        f"Missing reciprocal reference: '{source_id}' lists '{target_id}' "
                        f"in {source_section}, but {inverse_section} here does not reference "
                        f"'{source_id}'"
                    ),
                    severity="warning",
                ))

    return results


def load_schema(path: str | Path) -> dict:
    """Load a schema file.

    Returns a single schema dict (legacy) or a multi-schema dict.
    Multi-schema files have a top-level ``schemas:`` key mapping
    document_type names to individual schema definitions.  Shared
    ``value_types`` and ``definitions`` are merged into each
    sub-schema so that ``SchemaValidator`` works unchanged.
    """
    with open(path) as f:
        raw = yaml.safe_load(f)
    if "schemas" not in raw:
        return raw
    # Multi-schema: propagate shared keys into each sub-schema
    shared_value_types = raw.get("value_types", {})
    shared_definitions = raw.get("definitions", {})
    schemas = {}
    for doc_type, schema in raw["schemas"].items():
        merged_vt = {**shared_value_types, **schema.get("value_types", {})}
        merged_df = {**shared_definitions, **schema.get("definitions", {})}
        schema = {**schema, "document": doc_type}
        if merged_vt:
            schema["value_types"] = merged_vt
        if merged_df:
            schema["definitions"] = merged_df
        schemas[doc_type] = schema
    return {"_multi": True, "schemas": schemas}


# ════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════

def results_to_json(results: dict[str, list[ValidationError]]) -> str:
    """Render validation results as a JSON array of findings.

    Each finding is an object with ``file``, ``path``, ``message`` and
    ``severity``. Clean files contribute nothing, so an empty array means
    everything validated.
    """
    findings = [
        {"file": fp, "path": err.path, "message": err.message, "severity": err.severity}
        for fp, errors in results.items()
        for err in errors
    ]
    return json.dumps(findings, indent=2)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Validate markdown against schema")
    parser.add_argument("schema", help="Schema YAML file")
    parser.add_argument("files", nargs="+", help="Markdown files to validate")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--json", action="store_true",
                        help="Emit findings as a JSON array (for editors/CI)")
    args = parser.parse_args()

    # Filter to existing files
    exit_code = 0
    valid_files = []
    for filepath in args.files:
        if not Path(filepath).exists():
            if not args.json:
                print(f"  ERROR: File not found: {filepath}")
            exit_code = 1
        else:
            valid_files.append(filepath)

    if not valid_files:
        if args.json:
            print("[]")
        sys.exit(exit_code)

    # Use cross-document validation when multiple files are provided
    results = validate_files(args.schema, valid_files)

    if args.json:
        print(results_to_json(results))
        if any(e.severity == "error" for errs in results.values() for e in errs):
            exit_code = 1
        sys.exit(exit_code)

    for filepath, errors in results.items():
        if errors or not args.quiet:
            print(f"\n{'─' * 60}")
            print(f"  {filepath}")
            print(f"{'─' * 60}")

        if not errors:
            if not args.quiet:
                print("  ✓ Valid")
        else:
            for err in errors:
                icon = "✗" if err.severity == "error" else "⚠"
                print(f"  {icon} [{err.path}] {err.message}")
            if any(e.severity == "error" for e in errors):
                exit_code = 1

    print()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
