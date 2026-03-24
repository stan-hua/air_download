"""Filtering utilities for AIR API results."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
