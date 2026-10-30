"""Document parsing for binary formats the model cannot read as text."""

from chagent.documents.parsers import (
    BINARY_SUFFIXES,
    Block,
    DocumentError,
    LEGACY_SUFFIXES,
    MissingDependencyError,
    ParseCache,
    is_binary_document,
    parse,
    parse_with_cache,
    suffix_of,
)

__all__ = [
    "BINARY_SUFFIXES",
    "Block",
    "DocumentError",
    "LEGACY_SUFFIXES",
    "MissingDependencyError",
    "ParseCache",
    "is_binary_document",
    "parse",
    "parse_with_cache",
    "suffix_of",
]
