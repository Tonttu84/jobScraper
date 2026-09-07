"""Source adapter interface.

A source is a callable object: ``fetch(ctx) -> Iterable[Job]``. Adapters must never raise on
a single bad record — log and skip — but should raise ``SourceHTTPError`` (or any exception)
when the *endpoint* is broken, so ``jobscraper probe`` can report "this site changed".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from jobscraper.config import Profile
from jobscraper.http import Http
from jobscraper.models import Job

log = logging.getLogger(__name__)


@dataclass
class SourceContext:
    http: Http
    profile: Profile
    options: dict[str, Any] = field(default_factory=dict)
    limit: int | None = None  # cap results (used by `probe`)

    def opt(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)


class Source(Protocol):
    name: str
    description: str

    def fetch(self, ctx: SourceContext) -> Iterable[Job]: ...


_REGISTRY: dict[str, Source] = {}


def register(source: Source) -> Source:
    _REGISTRY[source.name] = source
    return source


def all_sources() -> dict[str, Source]:
    # Import adapters lazily so a broken optional dependency doesn't kill the CLI.
    from jobscraper.sources import _load_all

    _load_all()
    return dict(_REGISTRY)


def get_source(name: str) -> Source:
    sources = all_sources()
    if name not in sources:
        raise KeyError(f"unknown source {name!r}; known: {', '.join(sorted(sources))}")
    return sources[name]


def take(items: Iterable[Job], limit: int | None) -> Iterable[Job]:
    if limit is None:
        yield from items
        return
    for i, item in enumerate(items):
        if i >= limit:
            return
        yield item


def safe_records(records: Iterable[Any], convert: Callable[[Any], Job | None], source: str) -> Iterable[Job]:
    """Convert raw records, skipping (and logging) any that fail to normalize."""
    for rec in records:
        try:
            job = convert(rec)
        except Exception as exc:
            log.warning("%s: skipping record (%s): %r", source, exc, str(rec)[:200])
            continue
        if job is not None:
            yield job
