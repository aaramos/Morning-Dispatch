"""Wiring guarantees for the academic / regulatory / hacker_news lanes."""

from __future__ import annotations

import pytest

from backend.agents.discovery import types as discovery_types
from backend.agents.discovery.registry import default_source_registry
from backend.app.services import brief_settings
from backend.app.services.profile_patch import VALID_SOURCE_ADAPTERS as PATCH_VALID
from backend.app.services.source_window import _adapter_from_payload_type

NEW_SOURCES = ("academic", "regulatory", "hacker_news")


@pytest.mark.parametrize("name", NEW_SOURCES)
def test_registered_in_registry(name: str) -> None:
    assert name in default_source_registry().names()


@pytest.mark.parametrize("name", NEW_SOURCES)
def test_present_in_valid_adapter_sets(name: str) -> None:
    assert name in discovery_types.VALID_SOURCE_ADAPTERS
    assert name in PATCH_VALID
    assert name in discovery_types.DEFAULT_SOURCE_SELECTION
    assert name in discovery_types.DEFAULT_EXPLORE_SOURCE_SELECTION


@pytest.mark.parametrize("name", NEW_SOURCES)
def test_default_off(name: str) -> None:
    assert discovery_types.DEFAULT_SOURCE_SELECTION[name] is False
    assert discovery_types.DEFAULT_EXPLORE_SOURCE_SELECTION[name] is False


def test_per_source_caps() -> None:
    assert brief_settings.source_inclusion_max("academic") == 50
    assert brief_settings.source_inclusion_max("regulatory") == 50
    assert brief_settings.source_inclusion_max("hacker_news") == 40


@pytest.mark.parametrize(
    "source_type,adapter",
    [
        ("academic_paper", "academic"),
        ("regulatory_filing", "regulatory"),
        ("hacker_news_story", "hacker_news"),
    ],
)
def test_source_type_maps_to_adapter(source_type: str, adapter: str) -> None:
    assert _adapter_from_payload_type(source_type) == adapter
