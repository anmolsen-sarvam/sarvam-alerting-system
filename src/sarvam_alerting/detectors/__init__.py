"""Detector registry."""

from __future__ import annotations

from .base import Detector, DetectorContext
from .connectivity import ConnectivityDetector
from .conversationality import ConversationalityDetector
from .disposition_accuracy import DispositionAccuracyDetector
from .errors import ErrorRateDetector
from .expected_values import ExpectedValuesDetector
from .insights_quality import InsightsQualityDetector
from .required_populated import RequiredPopulatedDetector
from .short_calls import ShortCallsDetector
from .value_sanity import ValueSanityDetector
from .variable_collapse import VariableCollapseDetector

# Registry of all known detectors, keyed by their config name.
DETECTOR_CLASSES: tuple[type[Detector], ...] = (
    VariableCollapseDetector,
    ValueSanityDetector,
    RequiredPopulatedDetector,
    ExpectedValuesDetector,
    ConnectivityDetector,
    ShortCallsDetector,
    ErrorRateDetector,
    ConversationalityDetector,
    DispositionAccuracyDetector,
    InsightsQualityDetector,
)


def build_detectors(detector_config: dict) -> list[Detector]:
    """Instantiate every enabled detector from the [detectors.*] config."""
    detectors: list[Detector] = []
    for cls in DETECTOR_CLASSES:
        options = dict(detector_config.get(cls.name, {}))
        detector = cls(options)
        if detector.enabled:
            detectors.append(detector)
    return detectors


__all__ = [
    "Detector",
    "DetectorContext",
    "DETECTOR_CLASSES",
    "build_detectors",
    "VariableCollapseDetector",
    "ValueSanityDetector",
    "RequiredPopulatedDetector",
    "ExpectedValuesDetector",
    "ConnectivityDetector",
    "ShortCallsDetector",
    "ErrorRateDetector",
    "ConversationalityDetector",
    "DispositionAccuracyDetector",
    "InsightsQualityDetector",
]
