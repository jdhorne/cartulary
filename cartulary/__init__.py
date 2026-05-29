"""cartulary: Validate a corpus of structured Markdown against a YAML schema,
with cross-document referential integrity."""

from .validator import validate_file, validate_files, load_schema, SchemaValidator, ValidationError
