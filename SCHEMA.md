# cartulary — Schema Specification

**Version 0.1** (draft)

This document is the **specification for the schema format**: the YAML you
write to describe what a valid Markdown document (or corpus of documents)
looks like. For a tour and motivation, see [README.md](README.md). For
worked examples, see [`examples/`](examples/).

The Python package in this repository is the **reference implementation** of
this spec. The spec's observable behaviour is pinned by a language-neutral
**[conformance suite](conformance/)** — a set of `{schema, documents, expected
findings}` cases that any implementation, in any language, can run to prove it
conforms. The canonical contract is the set of `(document, path, severity)`
findings; see [conformance/README.md](conformance/README.md).

A schema is a YAML file. It comes in two shapes:

- **Single-schema** — the file *is* one schema. Use it when a corpus holds
  one kind of document. See [`examples/note.schema.yaml`](examples/note.schema.yaml).
- **Multi-schema** — the file has a top-level `schemas:` map, one entry per
  document type. Each document selects its schema via a `document_type`
  frontmatter field. See [`examples/library.schema.yaml`](examples/library.schema.yaml).

---

## Top-level keys

| Key | Applies to | Meaning |
|-----|-----------|---------|
| `value_types` | both | Named, reusable type definitions (see below). In multi-schema files, declared once at the top and merged into every sub-schema. |
| `definitions` | both | Free-form shared block, merged into every sub-schema (for your own anchors/reuse). |
| `schemas` | multi only | Map of `document_type` → sub-schema. Presence of this key is what makes a file multi-schema. |
| `frontmatter` | per schema | Frontmatter field rules (see [Frontmatter](#frontmatter)). |
| `title_pattern` | per schema | Expected H1 title, with `{field}` placeholders (see [Title](#title)). |
| `sections` | per schema | Ordered list of section rules (see [Sections](#sections)). |
| `primary_key` | per schema | Name of the frontmatter field that uniquely identifies the document. Equivalent to setting `primary_key: true` on the field. |
| `filename_must_match` | per schema | Name of a frontmatter field whose value must equal the file's stem (filename without extension). |
| `additional_sections` | per schema | `false` (default) errors on unknown sections; `"warn"` warns; `true` allows them. |
| `additional_subsections` | per schema | Document-level default for unknown subsections; overridable per section. |

---

## value_types

Reusable type definitions referenced by name from any field, column, or item
via `type: <name>`. A type definition supports one of:

```yaml
value_types:
  year:
    description: "Four-digit year"      # used in error messages
    pattern: "^\\d{4}$"                 # regex; value must match
    examples: ["1937", "2026"]          # documentation only

  status:
    enum: ["in-print", "out-of-print"]  # value must be one of these

  reference_or_literal:
    any_of:                             # value matches if ANY option matches
      - literal: "Unknown"              #   exact-string option
      - type: year                      #   another value_type by name

  cover_image:
    pattern: "^covers/[\\w./-]+$"
    exists:                             # also assert the file exists on disk
      relative_to: "."                  #   dir(s) to resolve against, relative
                                        #   to the validated file (str or list)
      severity: warning                 #   "error" | "warning" (default warning)
```

A field/column whose `type` names an unknown value_type is **not** an error —
it simply isn't constrained. A bare `type: string` (or no `type`) means "any
non-empty string".

---

## Frontmatter

```yaml
frontmatter:
  fields:
    book_id:
      type: book_id_format     # a value_type name
      required: true           # missing/empty → error
      primary_key: true        # this field is the document's unique id
    status:
      enum: ["a", "b"]         # inline enum (no value_type needed)
    document_type:
      value: book              # must equal exactly this literal
    isbn:
      type: isbn               # optional (required not set)
      ref: author_id           # this value points at another document's PK
```

Per-field keys:

- `type` — a `value_type` name (or `string`).
- `required` — `true` makes a missing or empty value an error.
- `value` — the value must equal this exact literal.
- `enum` — inline list of allowed values (alternative to a value_type enum).
- `primary_key` — marks this field as the document's unique identifier.
- `ref` — names the **primary-key field of the document type this value points
  to**. Used for cross-document resolution (see [References](#references)).

### List-valued fields

If a frontmatter value is a YAML list, validate each element with `items`:

```yaml
tags:
  items:
    type: slug              # every element must be a slug

links:
  items:
    any_of:                 # each element matches the first option that fits
      - { type: slug }      #   a bare scalar, OR …
      - type: object        #   … an object with named sub-fields
        fields:
          label: { required: true }
          url: { required: true }
```

`items` shapes: a scalar spec (`type`/`enum`/`value`/`ref`), an
`type: object` with `fields:`, or `any_of:` of either.

---

## Title

```yaml
title_pattern: "{title} ({year})"
```

The document's H1 (`# …`) is compared against the pattern with `{field}`
placeholders substituted from frontmatter. An optional `~` is permitted
immediately before any substituted value (an "approximately" convention),
so `# Thing ~1850` satisfies `Thing {year}` when `year: 1850`.

---

## Sections

`sections` is an **ordered** list. Each entry describes one heading (by
default H2) and, optionally, its content and nested subsections.

```yaml
sections:
  - heading: Summary
    required: true            # true → error if missing; "warn" → warning
    content: { type: prose }
  - heading: Editions
    content:
      type: table
      # …
  - heading: Old Section
    deprecated: true          # present → warning to remove it
  - heading: Change Log
    position: last            # if present, must be the final section
```

Per-section keys: `heading`, `required` (`true`/`"warn"`), `deprecated`,
`position: last`, `content`, `subsections`, `additional_subsections`.

**Ordering** is enforced among the sections the schema knows about: the
relative order of known sections in the document must match schema order.
Unknown sections are governed by `additional_sections`.

### Subsections

A section may declare `subsections:` using the same shape (recursively, for
H4 under H3, etc.):

```yaml
  - heading: Parent
    subsections:
      - heading: First
        required: true
      - heading: Second
        content: { type: prose }
```

Unknown subsections follow `additional_subsections` (per-section, then
document-level, default `false` — except prose sections with no declared
subsections, which default to allowing them).

---

## Content types

A section's `content.type` selects how its body is validated.

### `prose`

Free text. The section must exist (if `required`) but its body is not
constrained.

### `table`

```yaml
content:
  type: table
  min_rows: 1                 # combined across all tables in the section
  columns:                    # column order must match exactly
    Year:   { type: year }
    Format: { enum: [hardcover, paperback, ebook, audiobook] }
    ISBN:   { type: isbn, nullable: true }   # empty / —, -, – allowed
```

Each cell is validated with the same machinery as a frontmatter field
(`type`/`enum`/`value`/`ref`). `nullable: true` permits an empty cell or an
em/en dash; `nullable: "warn"` warns instead of erroring; omitting it makes
an empty cell an error. Multiple tables in one section are concatenated.

### `ref_list`

A bullet list of cross-references. Two styles:

```yaml
# unlabeled — a flat list of references
content:
  type: ref_list
  style: unlabeled
  ref: author_id              # PK field of the target document type
  min_items: 1                # minimum non-"Unknown" items
  inverse: Books              # reciprocal section on the target (optional)
```

```yaml
# labeled — named slots, e.g. "**Parent:** Name → `id`"
content:
  type: ref_list
  style: labeled
  items:
    - label: Parent
      inverse: Children
      allow_unknown: false    # if false, a slot with no ref and not
                              # "Unknown" warns
```

List-item syntax the parser understands:

```markdown
- Plain Name → `target-id`                 (unlabeled)
- **Parent:** Plain Name → `target-id`     (labeled)
- Unknown                                  (sentinel: counts as "no reference")
```

The cross-reference is the back-ticked id after a `→` arrow. The literals
`Unknown` / `None known` are treated as "no reference present".

### `log`

A bullet list whose every item must match a regex:

```yaml
content:
  type: log
  entry_pattern: "^\\d{4}-\\d{2}-\\d{2}: .+"
```

---

## References

This is what distinguishes cartulary from frontmatter/structure validators:
**referential integrity across a corpus.**

- A `ref:` value names the **primary-key field of the target document type**.
  When you validate multiple files together (`validate_files` / passing
  several files on the CLI), every collected reference is checked against the
  set of all known primary keys; unresolved references are reported.
- A reference must resolve to the **right kind of document**. Because each PK
  field name belongs to a document type, `ref: author_id` means "must point at
  an `author`" — a value that happens to match the format and resolves to some
  *other* type (e.g. a `book`) is reported as an error, not silently accepted.
  (For this to be unambiguous, give each document type a distinct PK field
  name in a multi-schema file.)
- A `ref:` is also **format-validated** against the target PK's type, so a
  malformed id is caught even before resolution.
- `inverse: <section>` declares a **reciprocal** expectation: if document A's
  section lists B, then B's named `inverse` section must list A. Missing
  reciprocals are reported as warnings. This works in both directions and
  across document types (since all PKs share one namespace).
- **Duplicate primary keys** across the corpus are reported.

Single-file validation (`validate_file`) skips resolution and reciprocity
(there is no corpus to resolve against) but still does format-validation and
all structural checks.

---

## Severity

Every finding is an `error` or a `warning`. The CLI exits non-zero only if at
least one `error` is present. Several rules let you choose: `required: "warn"`,
`nullable: "warn"`, `additional_sections: "warn"`, `exists.severity`, and
inverse/reciprocity findings (always warnings).

---

## Finding paths

Every finding carries a `path` — a stable, structural locator for *where* the
problem is. Paths are part of the conformance contract: a conforming
implementation must emit the same path for the same problem. The grammar:

| Path | Refers to |
|------|-----------|
| `filename` | the file's name vs. `filename_must_match` |
| `title` | the H1 title vs. `title_pattern` |
| `frontmatter` | the frontmatter block as a whole (e.g. invalid YAML) |
| `frontmatter.<field>` | a single frontmatter field |
| `frontmatter.<field>[<i>]` | the *i*-th element of a list-valued field |
| `frontmatter.<field>[<i>].<sub>` | a sub-field of an object list item |
| `sections` | document-level section problems (missing required, order, unknown) |
| `section[<Heading>]` | a section as a whole (deprecated, position, content shape, missing reciprocal) |
| `section[<Heading>].row[<i>].<Column>` | a table cell |
| `section[<Heading>].item` | an item in an unlabeled reference list |
| `section[<Heading>].<Label>` | a slot in a labeled reference list |
| `section[<Heading>].entry[<i>]` | an entry in a log |

Indices (`<i>`) are zero-based. Headings, column names, and labels appear
verbatim. Nested sections reuse `section[<Heading>]` with the subsection's own
heading.
