"""okf-tools: validate, lint, migrate, and index Open Knowledge Format bundles."""

__version__ = "0.2.0"

from .indexer import build_indexes
from .lint import Finding, lint
from .migrate import MigrationResult, migrate
from .parser import (
    BundleSnapshot,
    Document,
    concepts,
    is_conventional_actor,
    iter_documents,
    normalize_verified,
    load_bundle,
    parse_document,
    split_frontmatter,
    trust_tier,
)
from .validate import Report, validate
from .results import execute, report_data
from .safety import DEFAULT_LIMITS, BundleError, Limits

__all__ = [
    "__version__",
    "build_indexes",
    "lint",
    "Finding",
    "migrate",
    "MigrationResult",
    "validate",
    "Report",
    "Document",
    "parse_document",
    "split_frontmatter",
    "iter_documents",
    "concepts",
    "normalize_verified",
    "trust_tier",
    "is_conventional_actor",
    "BundleSnapshot",
    "load_bundle",
    "Limits",
    "DEFAULT_LIMITS",
    "BundleError",
    "execute",
    "report_data",
]
