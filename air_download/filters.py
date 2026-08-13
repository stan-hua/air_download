"""Filtering utilities for AIR API results."""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Description patterns identifying an axial (transverse) series. Matched as
# whole words, so "THORAX" and "TRAUMA" do not count as axial.
DEFAULT_AXIAL_PATTERNS = "ax,axial,tra,trans,transverse"

# The API does not report slice thickness, so it is parsed out of the series
# description when stated. Values outside this range are treated as something
# other than a thickness (a field of view, a kernel number, a body part size).
MIN_SLICE_THICKNESS_MM = 0.1
MAX_SLICE_THICKNESS_MM = 20.0

_THICKNESS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mm\b", re.IGNORECASE)


def apply_inclusion_filter(
    items: list[dict[str, Any]], field_name: str, patterns: str | None
) -> list[dict[str, Any]]:
    """Filter items by partial match in the specified field.

    Applies case-insensitive partial matching using any of the comma-separated
    patterns (OR logic).

    Args:
        items: A list of dictionaries to be filtered.
        field_name: The key in the dictionaries to apply the filter on.
        patterns: A comma-separated string of patterns to filter by.
            If None or empty, items are returned unchanged.

    Returns:
        A list of dictionaries that match any of the patterns in the
        specified field.
    """
    if not patterns:
        return items
    pattern_list = [p.strip().lower() for p in patterns.split(",")]
    original_count = len(items)
    available = {i.get(field_name, "") for i in items}
    logger.info("Available %ss: %s", field_name, available)
    filtered = [
        i
        for i in items
        if i.get(field_name) and any(p in i[field_name].lower() for p in pattern_list)
    ]
    logger.info(
        "%s filter: from %d originally to %d.",
        field_name.capitalize(),
        original_count,
        len(filtered),
    )
    return filtered


def apply_exclusion_filter(
    items: list[dict[str, Any]], field_name: str, patterns: str | None
) -> list[dict[str, Any]]:
    """Filter items by excluding partial matches in the specified field.

    Applies case-insensitive partial matching to exclude items matching any of
    the comma-separated patterns (OR logic for exclusion).

    Args:
        items: A list of dictionaries to be filtered.
        field_name: The key in the dictionaries to apply the filter on.
        patterns: A comma-separated string of patterns to exclude.
            If None or empty, items are returned unchanged.

    Returns:
        A list of dictionaries that do NOT match any of the patterns in the
        specified field.
    """
    if not patterns:
        return items
    pattern_list = [p.strip().lower() for p in patterns.split(",")]
    original_count = len(items)
    available = {i.get(field_name, "") for i in items}
    logger.info("Available %ss: %s", field_name, available)
    filtered = [
        i
        for i in items
        if not (i.get(field_name) and any(p in i[field_name].lower() for p in pattern_list))
    ]
    logger.info(
        "%s exclusion filter: from %d originally to %d.",
        field_name.capitalize(),
        original_count,
        len(filtered),
    )
    return filtered


def parse_slice_thickness(description: str | None) -> float | None:
    """Extract a slice thickness in millimetres from a series description.

    The API exposes no thickness field, so it has to be read out of the
    description when the scanner protocol states one (e.g.
    ``"AXIAL 0.625MM STD"``). Values outside the plausible range are
    ignored, and the smallest plausible value wins when a description
    mentions several.

    Args:
        description: The series description, which may be None.

    Returns:
        The thickness in millimetres, or None if the description states none.
    """
    if not description:
        return None
    plausible = [
        value
        for value in (float(match) for match in _THICKNESS_RE.findall(description))
        if MIN_SLICE_THICKNESS_MM <= value <= MAX_SLICE_THICKNESS_MM
    ]
    return min(plausible) if plausible else None


def _modality(item: dict[str, Any]) -> str:
    """Return an item's modality, upper-cased, or an empty string."""
    return (item.get("modality") or "").strip().upper()


def is_axial(series: dict[str, Any], patterns: list[str]) -> bool:
    """Report whether a series description names an axial plane.

    Args:
        series: A series dictionary from the API.
        patterns: Lower-case plane names to look for.

    Returns:
        True if any pattern appears as a whole word in the description.
    """
    description = series.get("description") or ""
    return any(
        re.search(rf"\b{re.escape(pattern)}\b", description, re.IGNORECASE)
        for pattern in patterns
    )


def select_thinnest_axial(
    series: list[dict[str, Any]], patterns: list[str]
) -> dict[str, Any] | None:
    """Choose the axial series with the thinnest slices.

    Prefers a thickness stated in the description, breaking ties on image
    count. When no candidate states one, falls back to the highest image
    count, which stands in for thinner slices at equal coverage.

    Args:
        series: The series to choose among.
        patterns: Plane names identifying an axial series.

    Returns:
        The chosen series, or None if none of them are axial CT.
    """
    candidates = [
        s for s in series if _modality(s) in ("CT", "") and is_axial(s, patterns)
    ]
    if not candidates:
        return None

    measured = [
        (thickness, s)
        for thickness, s in (
            (parse_slice_thickness(s.get("description")), s) for s in candidates
        )
        if thickness is not None
    ]
    if measured:
        thickness, chosen = min(
            measured, key=lambda pair: (pair[0], -(pair[1].get("imageCount") or 0))
        )
        logger.info(
            "Thinnest axial series: '%s' (%.3gmm, %s images).",
            chosen.get("description", ""),
            thickness,
            chosen.get("imageCount", "?"),
        )
        return chosen

    chosen = max(candidates, key=lambda s: s.get("imageCount") or 0)
    logger.info(
        "No axial series states a slice thickness; falling back to image "
        "count. Chose '%s' (%s images) from %d candidate(s).",
        chosen.get("description", ""),
        chosen.get("imageCount", "?"),
        len(candidates),
    )
    return chosen


def parse_axial_patterns(axial_patterns: str) -> list[str]:
    """Split a comma-separated pattern string into lower-case plane names."""
    return [p.strip().lower() for p in axial_patterns.split(",") if p.strip()]


def keep_thinnest_axial(
    series: list[dict[str, Any]],
    axial_patterns: str = DEFAULT_AXIAL_PATTERNS,
) -> list[dict[str, Any]]:
    """Keep the thinnest axial series and nothing else.

    Structured reports are dropped along with scouts, reformats, and the
    thicker reconstructions: the reconstruction is the point of the
    selection, and an SR carries no image data. The axial series is
    identified by description, since plane and thickness are not exposed as
    fields.

    Args:
        series: The series to select from, in API order.
        axial_patterns: Comma-separated plane names identifying an axial
            series.

    Returns:
        A single-element list holding the chosen series, or an empty list
        when nothing qualifies as axial.
    """
    patterns = parse_axial_patterns(axial_patterns)
    chosen = select_thinnest_axial(series, patterns)

    if chosen is None:
        logger.warning(
            "No axial CT series matched %s among %d series. Nothing is kept "
            "for this exam, so no archive is written; widen "
            "--axial-patterns if the series are named differently.",
            patterns,
            len(series),
        )
        return []

    logger.info("Series selection: from %d to 1 (thinnest axial).", len(series))
    return [chosen]
