"""Public CV API composed from focused implementation modules."""

from __future__ import annotations

from jobsearch_skill.cv_build import CVBuildMixin
from jobsearch_skill.cv_customize import CVCustomizeMixin
from jobsearch_skill.cv_evidence import CVEvidenceMixin
from jobsearch_skill.cv_models import (
    CVBuildResult,
    CVEvidence,
    CVSelection,
    PreparedCV,
)
from jobsearch_skill.cv_prepare import CVPrepareMixin
from jobsearch_skill.cv_registry import CVRegistry


class CVService(CVCustomizeMixin, CVBuildMixin, CVPrepareMixin, CVEvidenceMixin):
    """Coordinate CV evidence, preparation, compilation, and verification."""


__all__ = [
    "CVBuildResult",
    "CVEvidence",
    "CVRegistry",
    "CVSelection",
    "CVService",
    "PreparedCV",
]
