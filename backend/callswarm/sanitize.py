"""Shared output sanitizer.

Applied at three choke points:

* inside the activity event emitter, before an event is persisted;
* as an ASGI hook over every JSON API response;
* on persistence of ``AgentRun.output_artifact`` and ``activity_summary``.

Two deterministic rules:

1. Anything that looks like an E.164 phone number is masked to
   ``+CC ••••• ••NNN`` — country code and last three digits survive, nothing else.
2. Text containing a reasoning-leak marker (``<thinking>``, "chain of thought",
   ...) is rejected by raising :class:`ReasoningLeakError`. The markers are
   configurable through ``REASONING_LEAK_MARKERS``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from callswarm.config.settings import DEFAULT_REASONING_LEAK_MARKERS

# A leading ``+``, a non-zero first digit, then 6-14 further digits optionally
# separated by single spaces, dots or hyphens (so "+91 98765 43210" is caught too).
_PHONE_RE = re.compile(r"\+[1-9](?:[ .\-]?\d){6,14}")
_MASK_DOTS = "••••• ••"


class ReasoningLeakError(ValueError):
    """Raised when outbound or persisted text contains private-reasoning markers."""

    def __init__(self, marker: str, context: str) -> None:
        self.marker = marker
        self.context = context
        super().__init__(f"reasoning-leak marker {marker!r} rejected in {context}")


def _country_code_length(digits: str) -> int:
    # Deterministic and intentionally coarse: NANP (1) and Russia/Kazakhstan (7)
    # are one digit; everything else is treated as two. Over-masking is safe.
    return 1 if digits[0] in "17" else 2


def mask_phone(match_text: str) -> str:
    """Mask a single phone-number string, keeping the country code and last three digits."""
    digits = re.sub(r"\D", "", match_text)
    cc = digits[: _country_code_length(digits)]
    return f"+{cc} {_MASK_DOTS}{digits[-3:]}"


def mask_phones(text: str) -> str:
    """Mask every E.164-looking number in ``text``."""
    return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), text)


def contains_phone(text: str) -> bool:
    return _PHONE_RE.search(text) is not None


class Sanitizer:
    """Deterministic masking plus reasoning-leak rejection."""

    def __init__(self, markers: Iterable[str] | None = None) -> None:
        raw = list(markers) if markers is not None else list(DEFAULT_REASONING_LEAK_MARKERS)
        self.markers: tuple[str, ...] = tuple(m for m in raw if m)
        self._lowered = tuple(m.lower() for m in self.markers)

    def find_reasoning_marker(self, text: str) -> str | None:
        lowered = text.lower()
        for marker, lowered_marker in zip(self.markers, self._lowered, strict=True):
            if lowered_marker in lowered:
                return marker
        return None

    def check_text(self, text: str, *, context: str) -> None:
        marker = self.find_reasoning_marker(text)
        if marker is not None:
            raise ReasoningLeakError(marker, context)

    def sanitize_text(self, text: str, *, context: str) -> str:
        self.check_text(text, context=context)
        return mask_phones(text)

    def sanitize_value(self, value: Any, *, context: str) -> Any:
        """Recursively sanitize strings inside JSON-like data (dicts, lists, scalars)."""
        if isinstance(value, str):
            return self.sanitize_text(value, context=context)
        if isinstance(value, dict):
            return {
                self.sanitize_text(str(k), context=context): self.sanitize_value(v, context=context)
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.sanitize_value(item, context=context) for item in value]
        return value


def sanitizer_from_markers(markers: Sequence[str] | None) -> Sanitizer:
    return Sanitizer(markers)
