from __future__ import annotations

from collections.abc import Iterable

from backend.agents.discovery.adapters import (
    CollectionsSourceAdapter,
    GmailSourceAdapter,
    MarketsSourceAdapter,
    PodcastSourceAdapter,
    WebSearchSourceAdapter,
    YouTubeSourceAdapter,
    RedditSourceAdapter,
)
from backend.agents.discovery.foreign_media import ForeignMediaSourceAdapter
from backend.agents.discovery.types import SourceAdapter


class SourceRegistry:
    def __init__(self, adapters: Iterable[SourceAdapter] = ()):
        self._adapters: dict[str, SourceAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: SourceAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def adapters(self) -> list[SourceAdapter]:
        return list(self._adapters.values())

    def selected(self, source_selection: dict[str, bool]) -> list[SourceAdapter]:
        return [
            adapter
            for adapter in self.adapters()
            if bool(source_selection.get(adapter.name, False))
        ]

    def names(self) -> list[str]:
        return list(self._adapters)


def default_source_registry() -> SourceRegistry:
    return SourceRegistry(
        [
            GmailSourceAdapter(),
            PodcastSourceAdapter(),
            WebSearchSourceAdapter(),
            ForeignMediaSourceAdapter(),
            YouTubeSourceAdapter(),
            CollectionsSourceAdapter(),
            MarketsSourceAdapter(),
            RedditSourceAdapter(),
        ]
    )
