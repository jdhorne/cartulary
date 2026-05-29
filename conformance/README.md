# cartulary conformance suite

A language-neutral test suite for the [cartulary schema specification](../SCHEMA.md).
It exists so that **any** implementation — not just the Python reference one in
this repo — can prove it conforms by running the same cases. If you write a
cartulary validator in another language, make it pass this suite.

Everything here is plain data (YAML). There is no code in the suite itself; the
reference implementation runs it via [`../tests/test_conformance.py`](../tests/test_conformance.py),
which is a thin ~40-line harness you can re-create in any language.

## Layout

```
conformance/
  schemas/      # the schemas cases validate against, by filename
  cases/        # the test cases, grouped into files; each file is a YAML list
```

## Case format

Each case is a YAML mapping:

```yaml
- name: unresolved-reference          # unique, human-readable
  description: ...                     # what behaviour this pins down
  schema: library.yaml                 # a file in conformance/schemas/
  documents:                           # one or more Markdown documents
    - path: lonely-2020.md             # logical filename (used for filename rules)
      content: |
        ---
        ...frontmatter...
        ---
        # ...markdown body...
  expect:                              # the findings the validator MUST produce
    - file: lonely-2020.md
      path: "section[Written By].item"
      severity: error                  # "error" | "warning"; defaults to error
      message_contains: "Unresolved"   # OPTIONAL — see "The contract" below
```

A case with `expect: []` asserts the documents validate cleanly.

## How to run it (any implementation)

For each case:

1. Write the schema (`conformance/schemas/<schema>`) and each document
   (`content` at `path`) into a working directory, preserving the `path`
   names — some rules (`filename_must_match`, on-disk `exists`) depend on them.
2. Validate **all** of the case's documents together as one corpus (the
   equivalent of the reference implementation's `validate_files`). Validating
   the set together is what enables cross-document reference checks.
3. Collect each finding as a `(document, path, severity)` triple.
4. The set of triples your validator produces must equal the set built from
   `expect` (treat it as a multiset / sorted list — order is not significant).

## The contract

- **Canonical identity of a finding is `(document, path, severity)`.** That is
  the portable contract. Two conforming implementations must agree on exactly
  which findings exist, where (`path`), and at what `severity`.
- **`path` strings are part of the contract.** They are stable, structural
  locators — e.g. `frontmatter.year`, `title`, `sections`,
  `section[Editions].row[0].ISBN`, `section[Written By].item`,
  `frontmatter.tags[0]`, `filename`. An implementation must emit the same path
  for the same problem. (The grammar of paths is documented in [SCHEMA.md](../SCHEMA.md).)
- **Messages are NOT part of the contract.** Human-readable wording is
  implementation-private. `message_contains`, where present, is an *extra*
  assertion the reference implementation makes against its own output; other
  implementations may ignore it (or use it as a loose sanity check).

## Adding a case

Drop it into the relevant file in `cases/` (or add a new file — every `*.yaml`
is discovered automatically). Keep cases **minimal**: include only the documents
and only the schema features needed to exercise one behaviour, so the `expect`
list stays short and the case reads as documentation of that rule.
