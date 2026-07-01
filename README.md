# cartulary

**Foreign keys for your Markdown docs: cross-document referential integrity, not just schema validation.**

`cartulary` validates a folder of Markdown files against a declarative YAML
schema. Plenty of tools check one file's frontmatter or heading structure;
cartulary's job is the part they don't: **referential integrity across the
whole set.** Declare a primary key, point `ref:` fields at it, and cartulary
resolves every cross-document reference, verifies the reciprocal link exists on
the other side, and flags dangling, malformed, and duplicate ids — across
*different document types* sharing one key namespace.

Think of it as *JSON Schema for an interlinked corpus of Markdown* — or, more
plainly, **foreign keys for your docs.**

> A *cartulary* is a medieval register that bound scattered charters into one
> cross-referenced, authoritative collection. This does the same for a folder
> of Markdown.

---

## Who it's for

- **Docs-as-code / knowledge bases** kept as plain Markdown in a repo, where
  pages reference each other (specs → ADRs, services → owners, terms → glossary)
  and you want CI to catch a broken or one-sided link.
- **Validating machine-generated Markdown** — when an LLM fills a fixed
  document template, cartulary checks the output actually conforms: required
  fields and sections, typed tables, *and* that the ids it emitted resolve and
  reciprocate. (None of the incumbents below target this.)
- **Anyone who has outgrown frontmatter-only validation** and wants the body —
  tables, reference lists, logs — held to a schema too.

If you only need single-file frontmatter or heading checks, the established
tools below are lighter and a better fit.

---

## What's novel — and what already exists

Being honest about the landscape, because most of it is solved:

| Capability | Already well-served by |
|---|---|
| Frontmatter against a schema | [remark-lint-frontmatter-schema], Astro + zod, JSON Schema |
| Required sections / heading order | [markdownlint] (rule **MD043**) |
| Cross-*file* link & heading existence | [remark-validate-links] |
| One file's body structure (headings/tables/lists) | [jackchuka/mdschema] |
| Cross-*collection* references (framework-bound) | [Astro `reference()`][astro-ref] |
| Reciprocal frontmatter links (one tool, one app) | [Obsidian *Sync Semantic Links*][obsidian-ssl] |

The first four are table stakes — cartulary does them, but they're not the
point. The last two are the closest prior art to cartulary's actual wedge, and
both stop short:

- **Astro `reference()`** resolves references between content collections — but
  it's bound to a JS framework and build step, and as of Astro 5 it **no longer
  checks that the referenced entry exists** ([withastro/astro#13268][astro-issue]),
  the exact guarantee cartulary is built to provide.
- **Obsidian's reciprocal-link tooling** mirrors relationships — but only over
  *frontmatter* properties, inside a single vault, as an editor plugin, not a
  schema you can run in CI.

What I could not find packaged anywhere is cartulary's combination: a
**standalone, language-agnostic, declarative** validator that does typed
primary-key reference *resolution* + **reciprocal (`inverse`) checking** +
duplicate-key detection across **heterogeneous document types** in one shared
namespace, spanning **both frontmatter and body** constructs (tables,
reference lists) — driven by a single YAML file and runnable over any folder in
any CI. That's the gap this fills.

[remark-lint-frontmatter-schema]: https://github.com/JulianCataldo/remark-lint-frontmatter-schema
[markdownlint]: https://github.com/DavidAnson/markdownlint/blob/main/doc/Rules.md
[remark-validate-links]: https://github.com/remarkjs/remark-validate-links
[jackchuka/mdschema]: https://github.com/jackchuka/mdschema
[astro-ref]: https://docs.astro.build/en/reference/modules/astro-content/
[astro-issue]: https://github.com/withastro/astro/issues/13268
[obsidian-ssl]: https://mcpmarket.com/tools/skills/sync-semantic-links-for-obsidian

---

## How it works

A small compiler pipeline:

```
markdown text
   │  marko (GFM)            lex + parse
   ▼
flat AST
   │  SectionTreeBuilder     impose the heading hierarchy marko doesn't
   ▼
section tree
   │  SchemaValidator        semantic analysis against the YAML schema
   ▼
list of (path, message, severity) findings
```

Cross-document checks run as a second and third pass over the whole file set:
collect every primary key and reference, resolve refs against known keys, then
verify reciprocity.

---

## Install

```bash
pip install cartulary     # from PyPI

pip install -e .          # or, from a clone (for development)
# runtime deps: marko, PyYAML
```

## Quickstart

```bash
# validate one or many files against a schema
python -m cartulary examples/library.schema.yaml tests/fixtures/*.md

# (after install) via the console script
cartulary examples/library.schema.yaml tests/fixtures/*.md

# machine-readable output for editors / CI
cartulary --json examples/library.schema.yaml docs/**/*.md

# SARIF 2.1.0 for GitHub code scanning (renders findings on the PR diff)
cartulary --sarif examples/library.schema.yaml docs/**/*.md > cartulary.sarif
```

Passing **multiple** files turns on cross-document reference checking.

```python
from cartulary import validate_file, validate_files

errors = validate_file("schema.yaml", "doc.md")          # single file
results = validate_files("schema.yaml", ["a.md", "b.md"]) # corpus, with refs
for err in errors:
    print(err.severity, err.path, err.message)
```

The CLI prints a per-file report and exits non-zero if any **error**
(as opposed to warning) is found.

---

## A taste of the schema

```yaml
# a book references its authors; each author must list the book back
schemas:
  book:
    primary_key: book_id
    title_pattern: "{title} ({year})"
    frontmatter:
      fields:
        document_type: { value: book, required: true }
        book_id:  { type: book_id_format, required: true }
        title:    { required: true }
        year:     { type: year, required: true }
    sections:
      - heading: Summary
        required: true
        content: { type: prose }
      - heading: Written By
        content:
          type: ref_list
          style: unlabeled
          ref: author_id        # foreign key → author primary key
          inverse: Books         # author's "Books" section must list this book
  author:
    frontmatter:
      fields:
        document_type: { value: author, required: true }
        author_id: { type: author_id_format, required: true, primary_key: true }
        name:      { required: true }
    sections:
      - heading: Books
        content: { type: ref_list, style: unlabeled, ref: book_id, inverse: Written By }
```

See [`examples/`](examples/) for three worked schemas — a multi-type library
catalogue, a single-schema note format, and a **[family tree](examples/family-tree.schema.yaml)**
(the use case this began as: every person is a file, and every parent/child/
spouse link must reciprocate) — and **[SCHEMA.md](SCHEMA.md)** for the complete
format reference.

The family-tree corpus is the quickest way to *see* the headline feature. It
validates clean:

```console
$ cartulary examples/family-tree.schema.yaml examples/family-tree/*.md
  ✓ Valid   (×7)
```

…but make a one-sided edit — say, remove Bilbo from his father's `Children`
while he still lists his father under `Parents` — and cartulary catches the
dangling half of the relationship that a frontmatter or link checker can't:

```console
  ⚠ [section[Children]] Missing reciprocal reference: 'bilbo-baggins' lists
    'bungo-baggins' in Parents, but Children here does not reference 'bilbo-baggins'
```

---

## Features

- Frontmatter: required fields, exact-value, enums, regex/`value_type`s, and
  list-valued fields (`items`, including `any_of` and object items).
- `value_types`: named, reusable `pattern` / `enum` / `any_of` definitions,
  plus on-disk `exists` checks.
- Title: `{field}` substitution with an optional `~` "circa" allowance.
- Sections: required/deprecated, strict ordering, `position: last`,
  unknown-section policy, and recursive subsections.
- Content types: `prose`, typed `table` (per-column types, `nullable`,
  `min_rows`), `ref_list` (labeled & unlabeled, with `min_items`/`max_items`
  cardinality), and `log` (regex per entry).
- **Cross-document**: reference resolution (refs must resolve **and** point at
  the right document *type*), reciprocal (`inverse`) checks,
  duplicate-primary-key detection, and multi-schema routing by
  `document_type` over a shared key namespace.
- **Blast-radius scoping** (`--changed`): validate the whole corpus but report
  only the findings a set of changed files is responsible for — ideal for
  gating a PR (see below).
- **Output for CI & editors**: a per-file human report, `--json`, or `--sarif`
  (SARIF 2.1.0 — GitHub code scanning renders findings inline on the PR diff).
  Every finding carries a stable `rule` id (e.g. `unresolved-reference`,
  `missing-reciprocal`) so its identity survives message-wording changes.
- Every finding is an `error` or `warning`; many rules let you pick.

---

## Validating a change against the whole corpus

Referential integrity is a property of the **entire** graph, so cartulary
always reads the whole corpus — there's no correct way to check one file in
isolation. Two consequences trip people up:

- Validating a **single file on its own** is not a cheaper subset of the work —
  it's *wrong*. Its references to other documents look dangling (the id
  namespace is just that one file), and one-sided links from elsewhere are
  invisible. Always pass the whole corpus.
- A problem you introduce by editing file **A** is often reported on a
  **different** file **B**. Remove `bilbo` from his father's `Children` while
  Bilbo still lists that father under `Parents`, and the *missing reciprocal* is
  reported on the **father**, not on Bilbo — because the father is the side now
  missing a link.

That second point is why gating CI on "findings in the file I changed" would
miss exactly the breakage that edit caused. `--changed` solves it by reporting
every finding whose **blast radius** touches a changed file — including ones
attributed to a counterpart — and nothing else:

```bash
# Validate the whole corpus, but only report (and fail on) findings that the
# files changed in this PR are responsible for:
cartulary schema.yaml docs/ --changed "$(git diff --name-only origin/main)"
```

The whole corpus is still validated; `--changed` only scopes the **output** and
the **exit status**, so a repo with pre-existing findings elsewhere won't fail a
PR that didn't touch them. Each finding's blast radius is also exposed as
`caused_by` in `--json` output (and on `ValidationError`) for editor/CI use.

---

## CI & pre-commit

**pre-commit.** cartulary ships a hook. In your `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/jdhorne/cartulary
    rev: v0.1.1
    hooks:
      - id: cartulary
        args: [schema.yaml, docs/]   # your schema, then the corpus path(s)
```

The hook validates the **whole corpus** (not just the staged files — referential
integrity is a whole-graph property), and runs whenever a Markdown or YAML file
changes.

**GitHub Action.** A composite action runs cartulary and uploads findings to
**code scanning**, so they render inline on the PR diff:

```yaml
# .github/workflows/cartulary.yml
permissions:
  security-events: write        # required for the SARIF upload
jobs:
  cartulary:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: jdhorne/cartulary@v0.1.1
        with:
          schema: schema.yaml
          files: docs/
          # optional: only fail on / report findings the PR is responsible for
          changed: ${{ github.event_name == 'pull_request' && 'docs/' || '' }}
```

The action installs cartulary from its own checkout (no PyPI release needed),
emits SARIF, uploads it, and fails the job if any **error**-level finding is in
scope. Set `upload-sarif: "false"` to skip the upload (e.g. on forks).

---

## Specification & conformance

cartulary is a **specification**, and this Python package is its **reference
implementation**. The schema format is documented in **[SCHEMA.md](SCHEMA.md)**,
and its observable behaviour is pinned by a language-neutral **[conformance
suite](conformance/)** — `{schema, documents, expected-findings}` cases that any
implementation in any language can run to prove it conforms. The portable
contract is the set of `(document, path, severity)` findings; exact wording is
implementation-private. If you port cartulary to another language, make it pass
[`conformance/`](conformance/).

## Develop / test

```bash
pip install -e ".[dev]"
pytest
```

Two test layers: [tests/test_conformance.py](tests/test_conformance.py) runs the
portable conformance suite (the spec contract), and
[tests/test_validator.py](tests/test_validator.py) covers
implementation-specific internals.

## License

[MIT](LICENSE)
