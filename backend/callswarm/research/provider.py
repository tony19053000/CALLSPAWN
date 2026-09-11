"""The ResearchProvider protocol and its typed errors.

Every result a provider returns carries a :class:`Provenance` with a
mandatory ``source_type``; the models reject anything without one. Providers
never interpret retrieved text — that is data, and it is fenced with
``untrusted_block`` before any model sees it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from callswarm.models import RawPage, RawResult, ResearchQuery


class ResearchError(Exception):
    """Base class for research-layer errors."""


class ResearchNotConfigured(ResearchError):
    """A live provider was requested without the credentials it needs."""


class PageFetchRefused(ResearchError):
    """A page fetch was refused by policy (scheme, robots.txt, content type)."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"refused to fetch {url!r}: {reason}")


@runtime_checkable
class ResearchProvider(Protocol):
    """One pluggable research surface.

    ``name`` is what the capability report and activity events show; it must
    make the data's nature obvious (``fixture`` vs a live provider name).
    """

    name: str

    async def search(self, query: ResearchQuery) -> list[RawResult]: ...

    async def fetch_public_page(self, url: str) -> RawPage | None: ...
