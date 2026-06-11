from __future__ import annotations

import asyncio

import httpx

# Process-wide pool of httpx clients so discovery adapters reuse keep-alive
# connections instead of paying connection/TLS setup on every request. Keyed by
# purpose and bound to the event loop that created the client (a different loop
# — e.g. a fresh asyncio.run in tests — transparently gets its own client;
# stale/closed clients are replaced). Closed at app shutdown via
# aclose_shared_clients(). This generalizes the proven pattern from
# backend/agents/model/client.py.
_SHARED_CLIENTS: dict[str, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def shared_async_client(
    *,
    purpose: str,
    timeout: float | httpx.Timeout | None,
    follow_redirects: bool = False,
    http2: bool = False,
    headers: dict | None = None,
) -> httpx.AsyncClient:
    """Return a pooled httpx.AsyncClient for the given purpose.

    Lifecycle is owned by this module: callers must NOT close the returned
    client (no ``async with``). The configuration arguments (timeout,
    follow_redirects, http2, headers) only apply when the client for the
    purpose is first created on the current event loop; per-request ``timeout=``
    arguments at the call sites still apply as usual.
    """
    loop = asyncio.get_running_loop()
    existing = _SHARED_CLIENTS.get(purpose)
    if existing is not None:
        bound_loop, client = existing
        if bound_loop is loop and not client.is_closed:
            return client
    client = httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=follow_redirects,
        http2=http2,
        headers=headers,
        limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
    )
    _SHARED_CLIENTS[purpose] = (loop, client)
    return client


async def aclose_shared_clients() -> None:
    clients = list(_SHARED_CLIENTS.values())
    _SHARED_CLIENTS.clear()
    for _loop, client in clients:
        if hasattr(client, "is_closed") and not client.is_closed:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover - best-effort shutdown
                pass
