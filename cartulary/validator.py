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


class SchemaError(ValueError):
    """Raised by ``validate_files``/``validate_file`` in ``strict`` mode when the
    schema itself has errors (unknown/misspelled keys, undefined types, etc.).

    A malformed schema silently under-validates — the intended rule never runs —
    so strict callers get a hard failure instead of a false "clean" result. The
    schema findings are attached as ``.findings`` for inspection."""

    def __init__(self, findings: list["ValidationError"]):
        self.findings = findings
        detail = "; ".join(f"[{f.path}] {f.message}" for f in findings)
        super().__init__(f"invalid schema: {detail}")


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


DEFAULT_REFERENCE_ARROW = "→"
DEFAULT_UNKNOWN_LITERALS = ("Unknown", "unknown", "None known")

# Generic id shape used by the strikethrough fallback (below) when the schema
# declares no id pattern to derive from. Word characters and hyphens cover the
# common slug/snake/camel conventions without the caller having to configure it.
GENERIC_ID_PATTERN = r"[\w-]+"


def _strip_anchors(pattern: str) -> str:
    """Drop leading ``^`` / trailing ``$`` so a value_type pattern can be
    embedded *inside* a larger search regex (the id is mid-string in raw text,
    not the whole string). The anchors are zero-width, so removing them keeps
    the pattern valid."""
    if pattern.startswith("^"):
        pattern = pattern[1:]
    if pattern.endswith("$"):
        pattern = pattern[:-1]
    return pattern


def visit_list_item(node: ListItem, arrow: str = DEFAULT_REFERENCE_ARROW,
                    id_pattern: str | None = None) -> RefItem:
    """Extract label, name, and cross-reference from a list item.

    A cross-reference is a back-ticked id preceded by *arrow* (configurable via
    the schema's `conventions.reference_arrow`; defaults to "→").

    *id_pattern*, when given, is the (anchor-stripped) regex the target's
    primary key is declared to match; it is used only by the strikethrough
    fallback to recover an id from raw text using the schema's own id format
    rather than a hard-coded convention.
    """
    # Walk the inline children of the paragraph inside the list item
    item = RefItem(raw=extract_text(node).strip())
    arrow_re = re.escape(arrow)

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
                # CodeSpan after the arrow is a cross-reference
                preceding_text = extract_text(parts[i - 1]) if i > 0 else ""
                if arrow in preceding_text:
                    item.ref = part.children.strip() if isinstance(part.children, str) else extract_text(part).strip()

    # Fallback: GFM strikethrough can swallow the arrow + `ref` when tildes are
    # nearby, and extract_text strips backticks.  Try with and without backticks,
    # matching the id against the schema's declared id format when known so a
    # non-kebab convention (snake_case, single-token, uppercase…) isn't silently
    # dropped; degrade to a generic token if the pattern is absent or malformed.
    if item.ref is None:
        body = id_pattern or GENERIC_ID_PATTERN
        try:
            ref_m = re.search(arrow_re + r"\s*`?(" + body + r")`?", item.raw)
        except re.error:
            ref_m = re.search(arrow_re + r"\s*`?(" + GENERIC_ID_PATTERN + r")`?", item.raw)
        if ref_m:
            item.ref = ref_m.group(1).strip()

    # Extract name: everything that isn't the label or the ref
    name_text = item.raw
    if item.label:
        name_text = re.sub(r"\*\*\w[\w\s]*?:\*\*\s*", "", name_text)
    if item.ref:
        name_text = re.sub(r"\s*" + arrow_re + r"\s*`[^`]+`", "", name_text)
    item.name = name_text.strip() or None

    return item


def visit_list(node: List, arrow: str = DEFAULT_REFERENCE_ARROW,
               id_pattern: str | None = None) -> list[RefItem]:
    """Extract all items from a bullet/ordered list."""
    return [visit_list_item(item, arrow, id_pattern)
            for item in node.children if isinstance(item, ListItem)]


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
    # Stable machine identity for the *kind* of finding (e.g. "unresolved-reference").
    # Message wording may drift; the rule id is the durable handle used by --sarif
    # (as SARIF ruleId) and available for per-rule config/suppression. See RULES.
    rule: str = ""
    # Files whose content this finding depends on — its "blast radius". Always
    # includes the file the finding is reported on; a cross-document finding
    # (missing reciprocal, duplicate key) also includes the *other* files that
    # cause it, so editing any of them is relevant. Populated by validate_files;
    # empty for findings produced outside a corpus run.
    caused_by: set[str] = field(default_factory=set)


class SchemaValidator:
    """Validate a parsed markdown document against a YAML schema."""

    def __init__(self, schema: dict, known_ids: set[str] | None = None,
                 known_id_types: dict[str, str] | None = None,
                 ref_targets: dict[str, tuple[set[str], list[dict]]] | None = None,
                 ambiguous_ids: dict[str, set[str]] | None = None):
        self.schema = schema
        self.value_types = schema.get("value_types", {})
        # Document conventions (the micro-syntax for cross-references), overridable
        # per schema so the format is not tied to one arrow glyph or to English.
        conventions = schema.get("conventions", {})
        self.ref_arrow = conventions.get("reference_arrow", DEFAULT_REFERENCE_ARROW)
        self.unknown_literals = conventions.get(
            "unknown_literals", list(DEFAULT_UNKNOWN_LITERALS))
        self.known_ids = known_ids
        # Cross-document type awareness. ref_targets maps each valid `ref:` value
        # — a document type name, or (legacy) a PK field name — to the set of
        # document types it may resolve to and every one of those types' PK type
        # definitions, used to format-check it. Targeting a document type stays
        # precise even when two types share a PK field name; targeting a shared
        # field name degrades to "any owner of that field" — for format-checking
        # too, so a value valid for any one owner's PK type is accepted.
        self.known_id_types = known_id_types or {}
        self.ref_targets = ref_targets or {}
        # A duplicated primary key is an invalid-corpus condition: the id no
        # longer identifies a unique document, so it must never be used as a
        # resolution target. Maps each such ambiguous id to the set of files
        # colliding on it (used for the ambiguous-reference finding's
        # caused_by, alongside the referring file).
        self.ambiguous_ids = ambiguous_ids or {}
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
                self._error("filename", f"Filename '{stem}' does not match {match_field} '{field_val}'",
                            rule="filename-mismatch")

        # filename_pattern: a template matched against the *full* filename, with
        # {field} placeholders filled from frontmatter (generalises the stem-only
        # filename_must_match). e.g. "{slug}.md" or "{year}-{slug}.md".
        pattern = self.schema.get("filename_pattern")
        if filepath and isinstance(pattern, str):
            name = Path(filepath).name
            expected = re.sub(r"\{(\w+)\}",
                              lambda m: str(frontmatter.get(m.group(1), "")), pattern)
            if name != expected:
                self._error("filename",
                            f"Filename '{name}' does not match pattern '{pattern}' (expected '{expected}')",
                            rule="filename-mismatch")

        self._check_frontmatter(frontmatter)
        self._check_title(title, frontmatter)
        self._check_sections(sections)
        self._check_refs()
        return self.errors

    def _error(self, path: str, message: str, severity: str = "error", rule: str = "",
               caused_by: set[str] | None = None):
        self.errors.append(ValidationError(path=path, message=message,
                                           severity=severity, rule=rule,
                                           caused_by=set(caused_by) if caused_by else set()))

    # ── Cross-document ref checking ──────────────────────

    def _collect_ref(self, ref: str, path: str, ref_target: str | None = None):
        """Record a ref for cross-document resolution checking.

        If *ref_target* is a known target (a document type or PK field name),
        validate the ref value against that target's PK type definition(s). A
        legacy (PK-field-name) target can have several owners — document types
        that happen to share the field name — with *different* PK formats; the
        documented "degrades to any of them" resolution semantics must also
        hold for format-checking, so the value is accepted if it matches ANY
        owner's PK type, and rejected (naming the union) only if it matches
        none.
        """
        self.refs_found.append((path, ref, ref_target))
        target = self.ref_targets.get(ref_target) if ref_target else None
        if not target:
            return
        defns = [d for d in target[1] if d]
        if not defns:
            return
        if len(defns) == 1:
            self._check_value(ref, defns[0], path)
            return
        if any(self._value_passes(ref, d) for d in defns):
            return
        type_names = list(dict.fromkeys(d.get("type") for d in defns if d.get("type")))
        union = " or ".join(type_names) if type_names else "any owner"
        self._error(path, f"'{ref}' doesn't match any owner's type for '{ref_target}' ({union})",
                    rule="type-mismatch")

    def _id_pattern_for(self, ref_target: str | None) -> str | None:
        """The (anchor-stripped) regex an id of *ref_target*'s primary key is
        declared to match, or ``None`` if none is declarable.

        Used by the ``visit_list_item`` strikethrough fallback so a mangled
        reference is recovered using the schema's own id format rather than a
        hard-coded one. Prefers the cross-document ``ref_targets`` table (precise
        when ``ref:`` names a document type), and falls back to *this* schema's
        primary key — which is what the table holds in single-file validation,
        where it is not populated. When a legacy target has multiple owners,
        an arbitrary owner's pattern is used for this best-effort recovery
        heuristic (unlike format-checking, this is not the correctness-
        critical path — see _collect_ref for the any-of check).
        """
        pk_defn = None
        target = self.ref_targets.get(ref_target) if ref_target else None
        if target and target[1]:
            pk_defn = target[1][0]
        elif self._pk_defn:
            pk_defn = self._pk_defn
        if not pk_defn:
            return None
        type_def = self.value_types.get(pk_defn.get("type"))
        if not type_def or "pattern" not in type_def:
            return None
        return _strip_anchors(type_def["pattern"])

    def _check_refs(self):
        """If known_ids was provided, check that refs resolve — and, when the
        target's document type is known, that they resolve to the right *kind*
        of document (a `ref: author` — or legacy `ref: author_id` — must point
        at an author, not just at any id that happens to share the format)."""
        if self.known_ids is None:
            return
        for path, ref, ref_target in self.refs_found:
            if ref in self.ambiguous_ids:
                # The id exists but names more than one document — it is not
                # unresolved, and no particular colliding document is the
                # "real" target, so no reference-type conclusion is drawn.
                self._error(path, f"Reference '{ref}' is ambiguous: '{ref}' is a "
                                  f"duplicated primary key and cannot be resolved to a "
                                  f"unique document", rule="ambiguous-reference",
                            caused_by=self.ambiguous_ids[ref])
                continue
            if ref not in self.known_ids:
                self._error(path, f"Unresolved reference: '{ref}'", rule="unresolved-reference")
                continue
            target = self.ref_targets.get(ref_target) if ref_target else None
            expected = target[0] if target else None
            actual = self.known_id_types.get(ref)
            if expected and actual and actual not in expected:
                want = ", ".join(sorted(expected))
                self._error(path, f"Reference '{ref}' resolves to a "
                                  f"'{actual}' document, but a '{want}' was expected",
                            rule="reference-type")

    # ── Frontmatter ──────────────────────────────────────

    def _check_frontmatter(self, fm: dict):
        fields = self.schema.get("frontmatter", {}).get("fields", {})

        # Unknown-field policy, mirroring `additional_sections`: permissive by
        # default, or reject/`warn` on any field not declared above. `document_type`
        # is always exempt — it's the multi-schema routing field, not a content field.
        additional = self.schema.get("additional_fields", True)
        if additional is not True:
            for name in fm:
                if name in fields or name == "document_type":
                    continue
                severity = "warning" if additional == "warn" else "error"
                self._error(f"frontmatter.{name}", f"Unknown field '{name}'",
                            severity=severity, rule="unknown-field")

        for name, defn in fields.items():
            value = fm.get(name)
            # A primary_key field is implicitly required: a document with no
            # usable identity (missing, null, or empty-string PK) is invalid
            # on its own, regardless of whether the schema author also wrote
            # `required: true` on it. This is a single OR'd condition (not a
            # second check) so a field that is both `primary_key: true` and
            # `required: true` still yields exactly one finding.
            implicitly_required = name == self._pk_field
            if (defn.get("required") or implicitly_required) and (value is None or value == ""):
                self._error(f"frontmatter.{name}", f"Required field '{name}' is missing",
                            rule="required-field")
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
            self._error(path, f"Item does not match any_of options", rule="type-mismatch")
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
            self._error(path, f"Expected object (dict), got {type(item).__name__}",
                        rule="type-mismatch")
            return
        sub_fields = item_def.get("fields", {})
        for fname, fdef in sub_fields.items():
            fval = item.get(fname)
            sub_path = f"{path}.{fname}"
            if fdef.get("required") and (fval is None or fval == ""):
                self._error(sub_path, f"Required field '{fname}' is missing",
                            rule="required-field")
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
        """Pure shape check, no errors written — used for any_of option probing."""
        ok, _, _ = self._evaluate_value(value, defn)
        return ok

    # ── Title ────────────────────────────────────────────

    def _check_title(self, title: str | None, fm: dict):
        pattern = self.schema.get("title_pattern")
        if not pattern:
            return
        if title is None:
            self._error("title", "Missing H1 title", rule="title-missing")
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
            self._error("title", f"Title '{title}' does not match expected '{expected}'",
                        rule="title-mismatch")

    # ── Value checking ───────────────────────────────────

    def _evaluate_value(self, value: str, defn: dict) -> tuple[bool, str | None, str]:
        """Match *value* against a field/value_type definition.

        Pure: performs no I/O and writes no errors. Returns ``(ok, message, rule)``
        where ``message`` is the finding text and ``rule`` its taxonomy id on
        failure (both empty/None on success). This is the single source of truth
        for value/enum/type matching, shared by :meth:`_check_value` (which emits)
        and :meth:`_value_passes` (which only needs the boolean, for any_of option
        probing). The on-disk ``exists`` check lives in ``_check_value`` instead —
        it has a side effect and its own severity, so it is not part of the pure
        match.
        """
        if "value" in defn:
            if value != str(defn["value"]):
                return False, f"'{value}' must be '{defn['value']}'", "value-mismatch"
            return True, None, ""
        if "enum" in defn:
            if value not in [str(v) for v in defn["enum"]]:
                return False, f"'{value}' not in {defn['enum']}", "enum-mismatch"
            return True, None, ""

        type_name = defn.get("type")
        if not type_name or type_name == "string":
            return True, None, ""
        type_def = self.value_types.get(type_name)
        if not type_def:
            return True, None, ""

        if "pattern" in type_def:
            if not re.match(type_def["pattern"], value):
                desc = type_def.get("description", type_def["pattern"])
                return False, f"'{value}' doesn't match type '{type_name}' ({desc})", "type-mismatch"
            return True, None, ""
        if "enum" in type_def:
            if value not in [str(v) for v in type_def["enum"]]:
                return False, f"'{value}' not in {type_name} values {type_def['enum']}", "enum-mismatch"
            return True, None, ""
        if "any_of" in type_def:
            if not self._matches_any_of(value, type_def["any_of"]):
                return False, f"'{value}' doesn't match any option for type '{type_name}'", "type-mismatch"
            return True, None, ""
        return True, None, ""

    def _check_value(self, value: str, defn: dict, path: str):
        ok, message, rule = self._evaluate_value(value, defn)
        if not ok:
            self._error(path, message, rule=rule)
            return

        # On-disk existence check for `exists` value_types. Applies only to
        # pattern-or-bare types (an enum/any_of type returns above), matching the
        # original control flow; skipped when the pattern itself failed.
        type_name = defn.get("type")
        if not type_name or type_name == "string":
            return
        type_def = self.value_types.get(type_name)
        if not type_def or "enum" in type_def or "any_of" in type_def:
            return
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
                self._error(path, f"File not found: {value}", severity=severity,
                            rule="file-not-found")

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
                self._error("sections", f"Required section '{ss['heading']}' is missing",
                            severity=severity, rule="required-section")

        # Deprecated sections
        for ss in schema_sections:
            if ss.get("deprecated") and ss["heading"] in doc_headings:
                self._error(f"section[{ss['heading']}]", "Deprecated section — should be removed",
                            severity="warning", rule="deprecated-section")

        # Ordering
        doc_known = [h for h in doc_headings if h in schema_order]
        expected = [h for h in schema_order if h in doc_known]
        if doc_known != expected:
            self._error("sections", f"Section order: got {doc_known}, expected {expected}",
                        rule="section-order")

        # Position: last
        for ss in schema_sections:
            if ss.get("position") == "last" and ss["heading"] in doc_headings:
                if doc_headings[-1] != ss["heading"]:
                    self._error(f"section[{ss['heading']}]", f"Must be the last section",
                                rule="section-position")

        # Unknown sections
        additional = self.schema.get("additional_sections", False)
        schema_set = set(schema_order)
        for heading in doc_headings:
            if heading not in schema_set:
                if additional == "warn":
                    self._error("sections", f"Unknown section '{heading}'",
                                severity="warning", rule="unknown-section")
                elif not additional:
                    self._error("sections", f"Unknown section '{heading}'", rule="unknown-section")

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
                self._error(path, f"Required subsection '{ss['heading']}' is missing",
                            severity=severity, rule="required-section")

        # Deprecated subsections
        for ss in sub_schemas:
            if ss.get("deprecated") and ss["heading"] in sub_headings:
                self._error(path, f"Deprecated subsection '{ss['heading']}' — should be removed",
                            severity="warning", rule="deprecated-section")

        # Ordering (among known subsections)
        doc_known = [h for h in sub_headings if h in schema_sub_order]
        expected = [h for h in schema_sub_order if h in doc_known]
        if doc_known != expected:
            self._error(path, f"Subsection order: got {doc_known}, expected {expected}",
                        rule="section-order")

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
                    self._error(path, f"Unknown subsection '{heading}'",
                                severity="warning", rule="unknown-section")
                elif not additional:
                    self._error(path, f"Unknown subsection '{heading}'", rule="unknown-section")

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
                self._error(path, f"Expected a table with at least {defn['min_rows']} row(s)",
                            rule="table-missing")
            return

        expected_cols = list(defn.get("columns", {}).keys())
        min_rows = defn.get("min_rows", 0)
        col_defs = defn.get("columns", {})
        total_rows = 0

        for table_idx, table_node in enumerate(tables):
            td = visit_table(table_node)
            tpath = f"{path}.table[{table_idx}]" if len(tables) > 1 else path

            if td.columns != expected_cols:
                self._error(tpath, f"Columns {td.columns} don't match expected {expected_cols}",
                            rule="table-columns")

            total_rows += len(td.rows)

            for row_idx, row in enumerate(td.rows):
                for col_name, col_def in col_defs.items():
                    value = row.get(col_name, "")
                    cell_path = f"{tpath}.row[{row_idx}].{col_name}"
                    nullable = col_def.get("nullable", False)
                    if not value or value in ("—", "-", "–"):
                        if nullable == "warn":
                            self._error(cell_path, "Empty but not nullable",
                                        severity="warning", rule="cell-empty")
                        elif not nullable:
                            self._error(cell_path, "Empty but not nullable", rule="cell-empty")
                        continue
                    self._check_value(value, col_def, cell_path)
                    if col_def.get("ref"):
                        self._collect_ref(value, cell_path,
                                          ref_target=col_def["ref"])

        if total_rows < min_rows:
            self._error(path, f"Table(s) have {total_rows} row(s), need at least {min_rows}",
                        rule="table-min-rows")

    # ── Reference list content ───────────────────────────

    def _check_ref_list(self, section: Section, defn: dict, path: str):
        lists = [n for n in section.children if isinstance(n, List)]
        if not lists:
            if defn.get("style") == "labeled":
                self._error(path, "Expected a reference list but none found",
                            rule="ref-list-missing")
            elif defn.get("min_items", 0) > 0:
                self._error(path, f"Expected at least {defn['min_items']} item(s)",
                            rule="ref-cardinality")
            return

        style = defn.get("style", "unlabeled")
        ref_target = defn.get("ref")
        # Derive the target's declared id format up front so the strikethrough
        # fallback recovers ids in the schema's own convention, not a fixed one.
        id_pattern = self._id_pattern_for(ref_target)
        items: list[RefItem] = []
        for lst in lists:
            items.extend(visit_list(lst, self.ref_arrow, id_pattern))

        if style == "labeled":
            for expected in defn.get("items", []):
                label = expected["label"]
                matching = [it for it in items if it.label == label]
                if not matching:
                    self._error(f"{path}.{label}", f"Missing required item '{label}'",
                                rule="ref-item-missing")
                    continue
                item = matching[0]
                if item.ref is not None:
                    self._collect_ref(item.ref, f"{path}.{label}",
                                      ref_target=expected.get("ref", ref_target))
                    inverse = expected.get("inverse")
                    if inverse:
                        self.inverse_refs.append((section.heading, item.ref, inverse))
                elif not expected.get("allow_unknown"):
                    if item.name not in self.unknown_literals:
                        self._error(f"{path}.{label}",
                                    f"No cross-reference and not marked Unknown",
                                    severity="warning", rule="ref-missing")

        elif style == "unlabeled":
            real = [it for it in items if it.raw.strip() not in self.unknown_literals]
            min_items = defn.get("min_items", 0)
            max_items = defn.get("max_items")
            if len(real) < min_items:
                self._error(path, f"Need at least {min_items} item(s), found {len(real)}",
                            rule="ref-cardinality")
            if max_items is not None and len(real) > max_items:
                self._error(path, f"At most {max_items} item(s) allowed, found {len(real)}",
                            rule="ref-cardinality")
            inverse = defn.get("inverse")
            for item in real:
                if item.ref is not None:
                    self._collect_ref(item.ref, f"{path}.item",
                                      ref_target=ref_target)
                    if inverse:
                        self.inverse_refs.append((section.heading, item.ref, inverse))
                else:
                    self._error(f"{path}.item",
                                "No cross-reference and not marked Unknown",
                                severity="warning", rule="ref-missing")

    # ── Log content ──────────────────────────────────────

    def _check_log(self, section: Section, defn: dict, path: str):
        pattern = defn.get("entry_pattern")
        if not pattern:
            return
        lists = [n for n in section.children if isinstance(n, List)]
        if not lists:
            self._error(path, "Expected a log list but none found", rule="log-missing")
            return
        items = visit_list(lists[0])
        for idx, item in enumerate(items):
            if not re.match(pattern, item.raw):
                self._error(f"{path}.entry[{idx}]",
                            f"Doesn't match log format: '{item.raw}'", rule="log-format")


# ════════════════════════════════════════════════════════════
# Pipeline: tie it all together
# ════════════════════════════════════════════════════════════

def _extract_raw_title(body: str) -> str | None:
    """Extract H1 title from raw markdown before AST parsing.

    marko's GFM extension treats ~ as strikethrough, mangling titles
    like '# Name (~1842–~1901)'. We pull the title from raw text instead.

    Lines inside fenced code blocks are skipped so a '# ...' line in a fence
    is never mistaken for the title.
    """
    in_fence = False
    fence_marker = ""
    for line in body.splitlines():
        stripped = line.strip()
        if in_fence:
            # A fence closes on a line that starts with the same marker.
            if stripped.startswith(fence_marker):
                in_fence = False
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = True
            fence_marker = stripped[:3]
            continue
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


def _require_valid_schema(schema_path: str) -> None:
    """Precondition for document validation: the schema itself must be valid.

    Raises :class:`SchemaError` if schema meta-validation finds any error — you
    cannot meaningfully validate documents against an invalid contract, and a
    malformed schema silently under-validates (the intended rule never runs).
    To *inspect* schema findings without raising (editors, tooling, the CLI's
    report), call :func:`validate_schema` directly.
    """
    schema_errors = [f for f in validate_schema(schema_path) if f.severity == "error"]
    if schema_errors:
        raise SchemaError(schema_errors)


def validate_file(schema_path: str, filepath: str) -> list[ValidationError]:
    """Validate a single file (no cross-document ref checking).

    Raises :class:`SchemaError` if the schema itself is invalid (see
    :func:`_require_valid_schema`).
    """
    _require_valid_schema(schema_path)
    loaded = load_schema(schema_path)
    try:
        frontmatter, title, sections = _parse_file(filepath)
    except FrontmatterError as e:
        return [ValidationError(path="frontmatter",
                                message=f"Invalid YAML frontmatter: {e.detail}",
                                rule="invalid-frontmatter")]
    if loaded.get("_multi"):
        dt = frontmatter.get("document_type")
        schema = loaded["schemas"].get(str(dt)) if dt else None
        if schema is None:
            return [ValidationError(
                path="frontmatter.document_type",
                message=f"Unknown or missing document_type '{dt}'",
                rule="document-type",
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

    Raises :class:`SchemaError` if the schema itself is invalid (see
    :func:`_require_valid_schema`) — a malformed schema would silently
    under-validate the whole corpus, so it is a hard precondition, not a finding.

    Pass 1: parse all files, collect primary keys.
    Pass 2: validate each file with known_ids so unresolved refs
            are reported as warnings.
    Pass 3: check inverse ref reciprocity.
    """
    _require_valid_schema(schema_path)

    # De-duplicate by resolved path, preserving first-seen order — mirroring
    # the CLI's _expand_paths. Without this, a caller that (plausibly) repeats
    # a path — e.g. combining `git diff` output with a directory walk — would
    # have that file self-collide against a bogus "Duplicate primary key ...
    # (also in: )" with an empty also-in list, since the only "other" file is
    # itself (Adjudicator-found B). The CLI never hits this because
    # _expand_paths already dedupes; validate_files is the documented library
    # entry point and must agree.
    seen: set[str] = set()
    deduped_filepaths: list[str] = []
    for fp in filepaths:
        key = str(Path(fp).resolve())
        if key not in seen:
            seen.add(key)
            deduped_filepaths.append(fp)
    filepaths = deduped_filepaths

    loaded = load_schema(schema_path)
    multi = loaded.get("_multi", False)
    if multi:
        schemas = loaded["schemas"]
    else:
        # Single schema: wrap as multi with a synthetic key
        doc_type = loaded.get("document", "_default")
        schemas = {doc_type: loaded}

    # Build the PK field per schema and the ref-target table. A `ref:` value may
    # name a document type (precise) or a PK field name (legacy; degrades to
    # "any type owning that field" when several share it). ref_targets maps each
    # such name to (allowed document types, every one of those types' PK type
    # definitions) — a legacy target can have several owners with *different*
    # PK formats, and format-checking must accept a value valid for any owner,
    # not just whichever one happened to register first.
    pk_fields: dict[str, str | None] = {}
    ref_targets: dict[str, tuple[set[str], list[dict]]] = {}

    def _register_target(name: str, doc_type: str, defn: dict):
        owners, defns = ref_targets.setdefault(name, (set(), []))
        owners.add(doc_type)
        if defn not in defns:
            defns.append(defn)

    for doc_type, schema in schemas.items():
        pk_name = _resolve_pk_field(schema)
        pk_fields[doc_type] = pk_name
        if pk_name:
            fm_fields = schema.get("frontmatter", {}).get("fields", {})
            pk_defn = fm_fields.get(pk_name, {})
            _register_target(doc_type, doc_type, pk_defn)   # ref: <document type>
            _register_target(pk_name, doc_type, pk_defn)    # ref: <pk field> (legacy)

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
                rule="invalid-frontmatter",
            )]
            continue

        # Route to schema
        if multi:
            dt = frontmatter.get("document_type")
            if dt is None:
                results[fp] = [ValidationError(
                    path="frontmatter.document_type",
                    message="Missing 'document_type' field (required for multi-schema validation)",
                    rule="document-type",
                )]
                continue
            dt = str(dt)
            schema = schemas.get(dt)
            if schema is None:
                results[fp] = [ValidationError(
                    path="frontmatter.document_type",
                    message=f"Unknown document_type '{dt}' (expected one of: {', '.join(schemas.keys())})",
                    rule="document-type",
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

    # A duplicated primary key is an invalid-corpus condition: the id no
    # longer identifies a unique document, so downstream checks must never
    # use it as a resolution target — deterministically, regardless of
    # argument order. `ambiguous` maps each such id to every file colliding
    # on it (duplicate_ids already accumulates the full list per pk during
    # pass 1, independent of which file was "first"). It is excluded from
    # `id_to_type` so a stale first-seen type can never leak into the
    # reference-type check (belt-and-suspenders: `_check_refs` also short-
    # circuits on `ambiguous_ids` before consulting `known_id_types`).
    ambiguous: dict[str, set[str]] = {pk: set(files) for pk, files in duplicate_ids.items()}
    for pk in ambiguous:
        id_to_type.pop(pk, None)

    # Pass 2: validate each file, collect inverse refs
    inverse_index: dict[str, list[tuple[str, str, str]]] = {}
    ref_index: dict[str, set[tuple[str, str]]] = {}

    for fp, dt, schema, frontmatter, title, sections in parsed:
        validator = SchemaValidator(schema, known_ids=known_ids,
                                    known_id_types=id_to_type,
                                    ref_targets=ref_targets,
                                    ambiguous_ids=ambiguous)
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
                        rule="duplicate-key",
                        # The collision involves every file sharing this key.
                        caused_by={fp, *duplicate_ids[pk]},
                    ))

                # A document whose own key is ambiguous participates in no
                # reciprocity checking, as source or as target: its outbound
                # refs never populate ref_index (so an expectation targeting
                # it finds nothing to check against — see pass 3's `target_id
                # not in ref_index` skip) and its own inverse_refs never
                # become expectations (no reciprocity is asked of it either).
                if pk not in ambiguous:
                    ref_index[pk] = set()
                    # De-duplicate to at most one expectation per (this source,
                    # target, inverse section) triple: a target mentioned
                    # several times in the same inverse-bearing section (or
                    # from several sections sharing one `inverse:`) must yield
                    # exactly one missing-reciprocal finding, not one per
                    # mention. Sections whose `inverse:` differs are distinct
                    # triples and are kept separately. Order is the source
                    # document's own section/item order, which is independent
                    # of corpus/argument order, so this stays deterministic.
                    seen_inverse_triples: set[tuple[str, str]] = set()
                    for section_heading, target_id, inverse_section in validator.inverse_refs:
                        triple_key = (target_id, inverse_section)
                        if triple_key in seen_inverse_triples:
                            continue
                        seen_inverse_triples.add(triple_key)
                        inverse_index.setdefault(target_id, []).append(
                            (pk, section_heading, inverse_section)
                        )
                    for _path, ref, _target in validator.refs_found:
                        if _path.startswith("section["):
                            sec = _path.split("]")[0].removeprefix("section[")
                            ref_index[pk].add((sec, ref))
                            # Frontmatter refs are deliberately NOT added here: a
                            # section-level `inverse:` expectation must be satisfied
                            # only by the target's own inverse *section*, never by
                            # an unrelated frontmatter field — even one on a
                            # document whose section happens to be named
                            # "_frontmatter". ref_index is consumed only by pass 3
                            # (reciprocity); frontmatter refs still participate in
                            # resolution via refs_found / known_ids in _check_refs.

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
                # Reported on the target, but *caused* by both sides: editing the
                # source (drop the outbound link) or the target (add the back-link)
                # would resolve it, so both files are in scope.
                source_fp = id_to_file.get(source_id)
                caused = {target_fp} | ({source_fp} if source_fp else set())
                results[target_fp].append(ValidationError(
                    path=f"section[{inverse_section}]",
                    message=(
                        f"Missing reciprocal reference: '{source_id}' lists '{target_id}' "
                        f"in {source_section}, but {inverse_section} here does not reference "
                        f"'{source_id}'"
                    ),
                    severity="warning",
                    rule="missing-reciprocal",
                    caused_by=caused,
                ))

    # Every finding is caused, at minimum, by the file it is reported on. This
    # backfills the single-file case (structural/format findings) so blast-radius
    # scoping (see scope_to_changed) can treat all findings uniformly.
    for fp, errs in results.items():
        for e in errs:
            e.caused_by.add(fp)

    return results


def scope_to_changed(results: dict[str, list[ValidationError]],
                     changed: list[str]) -> dict[str, list[ValidationError]]:
    """Restrict corpus *results* to the blast radius of the *changed* files.

    A whole-corpus run must still happen — referential integrity is a property
    of the entire graph, and a one-sided link introduced by editing one file is
    reported on its *counterpart*, not on the edited file. This does not change
    what was validated; it filters the findings to those any *changed* file is
    responsible for, i.e. every finding whose ``caused_by`` set intersects the
    changed set. Paths are compared resolved, so relative and absolute spellings
    of the same file match. Files with no in-scope findings are dropped from the
    returned mapping.
    """
    changed_resolved = {str(Path(c).resolve()) for c in changed}

    def in_scope(err: ValidationError) -> bool:
        return any(str(Path(f).resolve()) in changed_resolved for f in err.caused_by)

    scoped: dict[str, list[ValidationError]] = {}
    for fp, errs in results.items():
        kept = [e for e in errs if in_scope(e)]
        if kept:
            scoped[fp] = kept
    return scoped


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
    shared_conventions = raw.get("conventions", {})
    schemas = {}
    for doc_type, schema in raw["schemas"].items():
        merged_vt = {**shared_value_types, **schema.get("value_types", {})}
        merged_df = {**shared_definitions, **schema.get("definitions", {})}
        merged_cv = {**shared_conventions, **schema.get("conventions", {})}
        schema = {**schema, "document": doc_type}
        if merged_vt:
            schema["value_types"] = merged_vt
        if merged_df:
            schema["definitions"] = merged_df
        if merged_cv:
            schema["conventions"] = merged_cv
        schemas[doc_type] = schema
    return {"_multi": True, "schemas": schemas}


# ════════════════════════════════════════════════════════════
# Schema meta-validation (catch mistakes in the schema itself)
# ════════════════════════════════════════════════════════════

_TOP_KEYS = {"value_types", "definitions", "frontmatter", "title_pattern", "sections",
             "primary_key", "filename_must_match", "filename_pattern", "additional_fields",
             "additional_sections", "additional_subsections", "conventions", "document"}
_FIELD_KEYS = {"required", "value", "enum", "type", "ref", "primary_key", "items"}
_ITEMS_KEYS = {"type", "fields", "any_of", "ref", "enum", "value", "required"}
_VALUE_TYPE_KEYS = {"description", "pattern", "enum", "any_of", "examples", "exists"}
_SECTION_KEYS = {"heading", "required", "deprecated", "position", "content",
                 "subsections", "additional_subsections"}
_CONTENT_KEYS = {
    "prose": {"type"},
    "table": {"type", "columns", "min_rows"},
    "ref_list": {"type", "style", "ref", "min_items", "max_items", "inverse", "items"},
    "log": {"type", "entry_pattern"},
}
_COLUMN_KEYS = {"type", "enum", "value", "ref", "nullable"}
_LABELED_ITEM_KEYS = {"label", "ref", "inverse", "allow_unknown"}
_CONVENTIONS_KEYS = {"reference_arrow", "unknown_literals"}


def validate_schema(path_or_dict) -> list[ValidationError]:
    """Validate a schema *itself* (not a document) and return findings.

    Catches the common authoring mistakes a document validator would otherwise
    swallow silently: misspelled keys (warnings), a ``type:`` naming an
    undefined value_type, an unknown content type, and ``primary_key`` /
    ``filename_must_match`` pointing at a non-existent field (errors).
    """
    if isinstance(path_or_dict, (str, Path)):
        with open(path_or_dict) as f:
            raw = yaml.safe_load(f)
    else:
        raw = path_or_dict

    errs: list[ValidationError] = []

    def err(p, m, sev="error", rule="schema-error"):
        errs.append(ValidationError(path=p, message=m, severity=sev, rule=rule))

    def unknown_keys(d, allowed, p):
        # An unknown or misplaced key means the rule the author intended never
        # runs — an error, not a warning. Keys prefixed `x-` are an escape hatch
        # for intentional annotations/extensions and are left alone.
        if isinstance(d, dict):
            for k in d:
                if k not in allowed and not str(k).startswith("x-"):
                    err(p, f"Unknown key '{k}'", "error", rule="unknown-schema-key")

    def check_conventions(cv, p):
        unknown_keys(cv, _CONVENTIONS_KEYS, p)

    def check_type_ref(type_name, p, vts):
        if isinstance(type_name, str) and type_name != "string" and type_name not in vts:
            err(p, f"type '{type_name}' is not a defined value_type")

    def check_field(fdef, p, vts):
        if not isinstance(fdef, dict):
            return
        unknown_keys(fdef, _FIELD_KEYS, p)
        check_type_ref(fdef.get("type"), p, vts)
        if isinstance(fdef.get("items"), dict):
            items = fdef["items"]
            unknown_keys(items, _ITEMS_KEYS, f"{p}.items")
            if "any_of" not in items and items.get("type") != "object":
                check_type_ref(items.get("type"), f"{p}.items", vts)

    def check_content(content, p, vts):
        if not isinstance(content, dict):
            return
        ctype = content.get("type")
        if ctype not in _CONTENT_KEYS:
            err(p, f"Unknown content type '{ctype}' (expected one of: "
                   f"{', '.join(sorted(_CONTENT_KEYS))})")
            return
        unknown_keys(content, _CONTENT_KEYS[ctype], p)
        for col, cdef in (content.get("columns") or {}).items():
            unknown_keys(cdef, _COLUMN_KEYS, f"{p}.columns.{col}")
            if isinstance(cdef, dict):
                check_type_ref(cdef.get("type"), f"{p}.columns.{col}", vts)
        if ctype == "ref_list":
            for j, it in enumerate(content.get("items") or []):
                unknown_keys(it, _LABELED_ITEM_KEYS, f"{p}.items[{j}]")

    def check_section(sec, p, vts):
        if not isinstance(sec, dict):
            return
        unknown_keys(sec, _SECTION_KEYS, p)
        if "content" in sec:
            check_content(sec["content"], f"{p}.content", vts)
        for j, sub in enumerate(sec.get("subsections") or []):
            check_section(sub, f"{p}.subsections[{j}]", vts)

    def check_one(schema, p, vts):
        unknown_keys(schema, _TOP_KEYS, p)
        check_conventions(schema.get("conventions"), f"{p}.conventions")
        for name, defn in (schema.get("value_types") or {}).items():
            unknown_keys(defn, _VALUE_TYPE_KEYS, f"{p}.value_types.{name}")
        fields = (schema.get("frontmatter") or {}).get("fields") or {}
        for fname, fdef in fields.items():
            check_field(fdef, f"{p}.frontmatter.{fname}", vts)
        pk = schema.get("primary_key")
        if isinstance(pk, str) and pk not in fields:
            err(f"{p}.primary_key", f"primary_key '{pk}' is not a defined frontmatter field")
        fmm = schema.get("filename_must_match")
        if isinstance(fmm, str) and fmm not in fields:
            err(f"{p}.filename_must_match",
                f"filename_must_match '{fmm}' is not a defined frontmatter field")
        fnp = schema.get("filename_pattern")
        if isinstance(fnp, str):
            for placeholder in re.findall(r"\{(\w+)\}", fnp):
                if placeholder not in fields:
                    err(f"{p}.filename_pattern",
                        f"filename_pattern references '{placeholder}', "
                        f"which is not a defined frontmatter field")
        for j, sec in enumerate(schema.get("sections") or []):
            check_section(sec, f"{p}.sections[{j}]", vts)

    if isinstance(raw, dict) and "schemas" in raw:
        unknown_keys(raw, {"schemas", "value_types", "definitions", "conventions"}, "schema")
        check_conventions(raw.get("conventions"), "schema.conventions")
        shared_vt = set((raw.get("value_types") or {}).keys())
        sub = raw.get("schemas")
        if not isinstance(sub, dict):
            err("schema.schemas", "'schemas' must be a mapping of document_type to schema")
        else:
            for dt, s in sub.items():
                vts = shared_vt | set((s.get("value_types") or {}).keys())
                check_one(s, f"schema:{dt}", vts)
    else:
        vts = set((raw.get("value_types") or {}).keys())
        check_one(raw, "schema", vts)

    return errs


# ════════════════════════════════════════════════════════════
# Finding taxonomy & SARIF
#
# Every document finding carries a stable ``rule`` id (see ValidationError).
# RULES maps each id to a human name + one-line description, used to populate a
# SARIF run's ``tool.driver.rules``. This is the machine-stable identity of a
# finding *kind*, distinct from its (drifting) message wording.
# ════════════════════════════════════════════════════════════

RULES: dict[str, tuple[str, str]] = {
    "filename-mismatch":    ("FilenameMismatch", "Filename does not match the field required by filename_must_match."),
    "required-field":       ("RequiredField", "A required frontmatter field (or object sub-field) is missing."),
    "unknown-field":        ("UnknownField", "A frontmatter field not declared in the schema (additional_fields)."),
    "value-mismatch":       ("ValueMismatch", "A value does not equal the required literal."),
    "enum-mismatch":        ("EnumMismatch", "A value is not one of the allowed enum options."),
    "type-mismatch":        ("TypeMismatch", "A value does not satisfy its declared value_type or item shape."),
    "file-not-found":       ("FileNotFound", "A value with an `exists` value_type points at a missing file."),
    "title-missing":        ("TitleMissing", "The document has no H1 title where one is required."),
    "title-mismatch":       ("TitleMismatch", "The H1 title does not match title_pattern."),
    "required-section":     ("RequiredSection", "A required section or subsection is missing."),
    "deprecated-section":   ("DeprecatedSection", "A section or subsection marked deprecated is present."),
    "section-order":        ("SectionOrder", "Sections or subsections are out of the schema's declared order."),
    "section-position":     ("SectionPosition", "A section constrained to `position: last` is not last."),
    "unknown-section":      ("UnknownSection", "A section or subsection not declared in the schema."),
    "table-missing":        ("TableMissing", "A required table is absent."),
    "table-columns":        ("TableColumns", "A table's columns do not match the declared columns."),
    "table-min-rows":       ("TableMinRows", "A table has fewer rows than min_rows."),
    "cell-empty":           ("CellEmpty", "A non-nullable table cell is empty."),
    "ref-list-missing":     ("RefListMissing", "A required reference list is absent."),
    "ref-item-missing":     ("RefItemMissing", "A required labeled reference-list item is missing."),
    "ref-missing":          ("RefMissing", "A labeled slot has no cross-reference and is not marked Unknown."),
    "ref-cardinality":      ("RefCardinality", "A reference list violates its min_items/max_items."),
    "log-missing":          ("LogMissing", "A required log list is absent."),
    "log-format":           ("LogFormat", "A log entry does not match entry_pattern."),
    "unresolved-reference": ("UnresolvedReference", "A ref does not resolve to any known primary key."),
    "reference-type":       ("ReferenceType", "A ref resolves to a document of the wrong type."),
    "duplicate-key":        ("DuplicateKey", "A primary key is used by more than one document."),
    "ambiguous-reference":  ("AmbiguousReference", "A ref's value equals a primary key duplicated across "
                                                    "documents, so it cannot be resolved to a unique target."),
    "missing-reciprocal":   ("MissingReciprocal", "A referenced document does not link back via its inverse section."),
    "invalid-frontmatter":  ("InvalidFrontmatter", "The YAML frontmatter block failed to parse."),
    "document-type":        ("DocumentType", "The document_type field is missing or unknown."),
    "unknown-schema-key":   ("UnknownSchemaKey", "The schema has an unknown or misplaced key, so the intended rule never runs."),
    "schema-error":         ("SchemaError", "The schema itself is invalid (bad type reference, malformed rule, etc.)."),
}

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"


def results_to_sarif(results: dict[str, list[ValidationError]],
                     tool_uri: str = "https://pypi.org/project/cartulary/") -> str:
    """Render validation results as a SARIF 2.1.0 log.

    Each finding becomes a ``result`` whose ``ruleId`` is the finding's stable
    rule id, ``level`` maps from severity (error/warning), the host file is the
    ``physicalLocation`` and the structural ``path`` is preserved as a
    ``logicalLocation`` (cartulary locates by structure, not line number, so no
    ``region`` is emitted). Only the rules that actually fired are declared in
    ``tool.driver.rules``. GitHub code scanning and SARIF-aware editors ingest
    this directly.
    """
    findings = [(fp, e) for fp, errs in results.items() for e in errs]

    used_ids: list[str] = []
    for _fp, e in findings:
        rid = e.rule or "unspecified"
        if rid not in used_ids:
            used_ids.append(rid)

    rules = []
    for rid in used_ids:
        name, desc = RULES.get(rid, ("Unspecified", "An unclassified finding."))
        rules.append({"id": rid, "name": name, "shortDescription": {"text": desc}})

    sarif_results = []
    for fp, e in findings:
        sarif_results.append({
            "ruleId": e.rule or "unspecified",
            "level": "error" if e.severity == "error" else "warning",
            "message": {"text": e.message},
            "locations": [{
                "physicalLocation": {"artifactLocation": {"uri": fp}},
                "logicalLocations": [{"fullyQualifiedName": e.path}],
            }],
        })

    doc = {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "cartulary",
                "informationUri": tool_uri,
                "rules": rules,
            }},
            "results": sarif_results,
        }],
    }
    return json.dumps(doc, indent=2)


# ════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════

def results_to_json(results: dict[str, list[ValidationError]]) -> str:
    """Render validation results as a JSON array of findings.

    Each finding is an object with ``file``, ``path``, ``message``,
    ``severity`` and ``caused_by`` (the files whose content the finding depends
    on — its blast radius). Clean files contribute nothing, so an empty array
    means everything validated.
    """
    findings = [
        {"file": fp, "path": err.path, "message": err.message,
         "severity": err.severity, "caused_by": sorted(err.caused_by)}
        for fp, errors in results.items()
        for err in errors
    ]
    return json.dumps(findings, indent=2)


_MARKDOWN_SUFFIXES = (".md", ".markdown")


def _expand_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """Expand CLI path arguments into a concrete list of markdown files.

    Each argument may be a file *or a directory*; directories are walked
    recursively for ``*.md`` / ``*.markdown`` files so ``cartulary schema.yaml
    docs/`` works without the caller relying on shell globstar. Explicitly named
    files are kept regardless of extension (the suffix filter applies only to
    directory walking). Returns ``(files, missing)`` where *files* is the
    deterministically sorted, de-duplicated set of existing files and *missing*
    lists arguments that do not exist.
    """
    files: list[str] = []
    missing: list[str] = []
    for arg in paths:
        p = Path(arg)
        if p.is_dir():
            files.extend(
                str(f) for f in sorted(p.rglob("*"))
                if f.is_file() and f.suffix.lower() in _MARKDOWN_SUFFIXES
            )
        elif p.exists():
            files.append(arg)
        else:
            missing.append(arg)
    # De-duplicate (a file may be named twice, or live under a directory that was
    # also passed) while preserving discovery order.
    seen: set[str] = set()
    deduped: list[str] = []
    for f in files:
        key = str(Path(f).resolve())
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    return deduped, missing


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Validate markdown against schema")
    parser.add_argument("schema", help="Schema YAML file")
    parser.add_argument("files", nargs="+", help="Markdown files to validate")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--json", action="store_true",
                        help="Emit findings as a JSON array (for editors/CI)")
    parser.add_argument("--sarif", action="store_true",
                        help="Emit findings as SARIF 2.1.0 (for GitHub code scanning "
                             "/ SARIF-aware editors)")
    parser.add_argument("--changed", action="append", metavar="FILE",
                        help="Report only findings in the blast radius of these "
                             "file(s) — those any changed file is responsible for, "
                             "including one-sided links reported on a counterpart. "
                             "Still validates the whole corpus; only scopes output "
                             "and exit status. Repeatable, or space/comma-separated "
                             "(e.g. --changed \"$(git diff --name-only)\").")
    args = parser.parse_args()

    # --json and --sarif are machine-output modes; both suppress the human report.
    machine = args.json or args.sarif

    # Validate the schema itself first; bad schemas are a usage error.
    schema_findings = validate_schema(args.schema)
    schema_errors = [f for f in schema_findings if f.severity == "error"]
    if schema_findings and not machine:
        print(f"\n  schema: {args.schema}")
        for f in schema_findings:
            icon = "✗" if f.severity == "error" else "⚠"
            print(f"  {icon} [{f.path}] {f.message}")
    if schema_errors:
        if args.sarif:
            print(results_to_sarif({args.schema: schema_findings}))
        elif args.json:
            print(json.dumps([
                {"file": args.schema, "path": f.path, "message": f.message,
                 "severity": f.severity} for f in schema_findings], indent=2))
        sys.exit(2)

    # Expand any directory arguments to the markdown files they contain.
    exit_code = 0
    valid_files, missing = _expand_paths(args.files)
    for filepath in missing:
        if not machine:
            print(f"  ERROR: File not found: {filepath}")
        exit_code = 1

    if not valid_files:
        if args.sarif:
            print(results_to_sarif({}))
        elif args.json:
            print("[]")
        sys.exit(exit_code)

    # Use cross-document validation when multiple files are provided
    results = validate_files(args.schema, valid_files)

    # Blast-radius scoping: the whole corpus is still validated (integrity is a
    # whole-graph property); --changed only narrows what is reported and gated on.
    if args.changed:
        changed = [p for group in args.changed for p in re.split(r"[,\s]+", group.strip()) if p]
        corpus_resolved = {str(Path(f).resolve()) for f in valid_files}
        for c in changed:
            if str(Path(c).resolve()) not in corpus_resolved:
                # Ignoring a non-matching --changed path is intentional (deletions
                # and renames legitimately appear in `git diff --name-only`), but
                # the signal must reach CI: always emit on stderr, in every output
                # mode, so stdout stays findings-only/machine-parseable while a
                # typo'd or stale --changed argument is never silent.
                print(f"  NOTE: --changed file is not in the validated set: {c}", file=sys.stderr)
        results = scope_to_changed(results, changed)

    if machine:
        print(results_to_sarif(results) if args.sarif else results_to_json(results))
        if any(e.severity == "error" for errs in results.values() for e in errs):
            exit_code = 1
        sys.exit(exit_code)

    if args.changed and not results:
        print("\n  ✓ No findings in the changed scope\n")
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
