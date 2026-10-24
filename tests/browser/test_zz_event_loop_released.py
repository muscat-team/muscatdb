"""The browser modules hand the main thread back without a running event loop.

Playwright's sync API keeps an asyncio loop marked as running for as long as
``sync_playwright()`` is open. A session-scoped ``browser`` fixture held it open
past the browser modules, and every later unit test calling ``asyncio.run()``
failed with "cannot be called from a running event loop". The ``zz`` prefix
runs this after the other browser modules, so it fails if one leaks the loop.
"""

from __future__ import annotations

import asyncio


async def _noop() -> int:
    return 1


def test_asyncio_run_works_after_browser_modules():
    assert asyncio.run(_noop()) == 1
