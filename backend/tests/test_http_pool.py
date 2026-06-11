from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.app.core import http_pool
from backend.app.core.http_pool import aclose_shared_clients, shared_async_client


@pytest.fixture(autouse=True)
def _clean_pool():
    http_pool._SHARED_CLIENTS.clear()
    yield
    clients = [client for _loop, client in http_pool._SHARED_CLIENTS.values()]
    http_pool._SHARED_CLIENTS.clear()
    for client in clients:
        if not client.is_closed:
            asyncio.run(client.aclose())


def test_shared_async_client_reuses_client_for_same_purpose() -> None:
    async def run() -> tuple[httpx.AsyncClient, httpx.AsyncClient, httpx.AsyncClient]:
        first = shared_async_client(purpose="web_search", timeout=8.0)
        second = shared_async_client(purpose="web_search", timeout=8.0)
        other = shared_async_client(purpose="youtube", timeout=10.0)
        return first, second, other

    first, second, other = asyncio.run(run())

    assert first is second
    assert other is not first


def test_shared_async_client_new_loop_gets_new_client() -> None:
    first = asyncio.run(_get_client())
    second = asyncio.run(_get_client())

    assert first is not second


async def _get_client() -> httpx.AsyncClient:
    return shared_async_client(purpose="web_search", timeout=8.0)


def test_shared_async_client_replaces_closed_client() -> None:
    async def run() -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
        first = shared_async_client(purpose="web_search", timeout=8.0)
        await first.aclose()
        second = shared_async_client(purpose="web_search", timeout=8.0)
        return first, second

    first, second = asyncio.run(run())

    assert first is not second
    assert first.is_closed
    assert not second.is_closed


def test_shared_async_client_applies_creation_options() -> None:
    async def run() -> httpx.AsyncClient:
        return shared_async_client(
            purpose="google_news",
            timeout=5.0,
            follow_redirects=True,
            headers={"X-Test": "1"},
        )

    client = asyncio.run(run())

    assert client.follow_redirects is True
    assert client.headers["X-Test"] == "1"


def test_aclose_shared_clients_closes_and_clears_pool() -> None:
    async def run() -> httpx.AsyncClient:
        client = shared_async_client(purpose="web_search", timeout=8.0)
        await aclose_shared_clients()
        return client

    client = asyncio.run(run())

    assert client.is_closed
    assert http_pool._SHARED_CLIENTS == {}
