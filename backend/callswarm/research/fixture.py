"""FixtureResearchProvider: labelled offline research data.

Loads every ``research.json`` under a directory (default ``scenarios/``).
Every result and page it returns is stamped ``source_type=FIXTURE`` and
``provider_name="fixture"`` by construction — there is no parameter that can
change that — and it performs no network I/O of any kind.

Fixture file format::

    {
      "results": [
        {"title": "...", "url": "...", "snippet": "...", "tags": ["..."],
         "data": {...any structured attributes...}}
      ],
      "pages": [{"url": "...", "title": "...", "text": "..."}]
    }

Search matches query tokens against a result's title, snippet and tags and
returns the best matches. The framework never inspects ``data`` keys.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from callswarm.models import Provenance, RawPage, RawResult, ResearchQuery, SourceType
from callswarm.research.fetch import PublicPageFetcher
from callswarm.research.provider import ResearchError
from callswarm.strategies.diversity import tokens

logger = logging.getLogger(__name__)

FIXTURE_PROVIDER_NAME = "fixture"
FIXTURE_FILENAME = "research.json"


class FixtureResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    url: str | None = None
    snippet: str = ""
    tags: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class FixturePage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    title: str = ""
    text: str = ""


class FixtureFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[FixtureResult] = Field(default_factory=list)
    pages: list[FixturePage] = Field(default_factory=list)


class FixtureLoadError(ResearchError):
    """A fixture file is missing or malformed."""


class FixtureResearchProvider:
    name = FIXTURE_PROVIDER_NAME
    source_type = SourceType.FIXTURE

    def __init__(self, fixture_dir: str | Path) -> None:
        self.fixture_dir = Path(fixture_dir)
        self._entries: list[tuple[str, FixtureResult]] = []
        self._pages: dict[str, tuple[str, FixturePage]] = {}
        self._load()

    # --- loading ---------------------------------------------------------------
    def _load(self) -> None:
        files = (
            sorted(self.fixture_dir.rglob(FIXTURE_FILENAME)) if self.fixture_dir.is_dir() else []
        )
        for path in files:
            try:
                parsed = FixtureFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, ValidationError) as exc:
                raise FixtureLoadError(f"invalid fixture {path}: {type(exc).__name__}") from exc
            ref = str(path.relative_to(self.fixture_dir))
            for index, result in enumerate(parsed.results):
                self._entries.append((f"{ref}#results[{index}]", result))
            for index, page in enumerate(parsed.pages):
                self._pages[page.url] = (f"{ref}#pages[{index}]", page)
        logger.info(
            "fixture research: %d result(s), %d page(s) from %d file(s) under %s",
            len(self._entries),
            len(self._pages),
            len(files),
            self.fixture_dir,
        )

    @property
    def result_count(self) -> int:
        return len(self._entries)

    # --- provider surface -------------------------------------------------------
    async def search(self, query: ResearchQuery) -> list[RawResult]:
        wanted = tokens(query.text)
        if query.kind_hint:
            wanted |= tokens(query.kind_hint)
        scored: list[tuple[int, int, str, FixtureResult]] = []
        for order, (ref, entry) in enumerate(self._entries):
            haystack = tokens(" ".join([entry.title, entry.snippet, *entry.tags]))
            score = len(wanted & haystack)
            if score:
                scored.append((-score, order, ref, entry))
        scored.sort()
        return [
            RawResult(
                title=entry.title,
                url=entry.url,
                snippet=entry.snippet,
                data=dict(entry.data),
                provenance=Provenance(
                    source_type=self.source_type,
                    provider_name=self.name,
                    source_url=entry.url,
                    reference=ref,
                    query=query.text,
                ),
            )
            for _, _, ref, entry in scored[: query.max_results]
        ]

    async def fetch_public_page(self, url: str) -> RawPage | None:
        PublicPageFetcher.check_scheme(url)  # same policy as live, still no I/O
        found = self._pages.get(url)
        if found is None:
            return None
        ref, page = found
        return RawPage(
            url=page.url,
            title=page.title,
            text=page.text,
            provenance=Provenance(
                source_type=self.source_type,
                provider_name=self.name,
                source_url=page.url,
                reference=ref,
            ),
        )
