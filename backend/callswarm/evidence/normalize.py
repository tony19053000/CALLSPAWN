"""Pure normalization helpers shared by the evidence, research and call layers.

Kept dependency-free so the evidence engine never imports the research
package at module load (research produces claims and depends on evidence,
not the other way round).
"""

from __future__ import annotations

import json
import re
from typing import Any


def normalize_key(key: str) -> str:
    """Lower-case snake_case with punctuation collapsed; units survive because
    they are alphanumeric (``"Price (INR)"`` → ``"price_inr"``)."""
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def canonical_value(value: Any) -> str:
    """Comparable form of a claim value. Numbers compare numerically, strings
    case-insensitively; structures by sorted JSON."""
    if isinstance(value, bool):
        return f"bool:{value}"
    if isinstance(value, int | float):
        return f"num:{float(value)}"
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return f"num:{float(stripped.replace(',', ''))}"
        except ValueError:
            return "str:" + " ".join(stripped.lower().split())
    return "json:" + json.dumps(value, sort_keys=True, default=str)
