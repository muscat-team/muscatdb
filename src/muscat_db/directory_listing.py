"""A StaticFiles variant that lists a directory's contents when it has no index.html.

Some externally-synced trees (e.g. the MuSCAT2 quicklook dashboard, see
``muscat_db.web._mount_muscat2_quicklook``) link to bare directories and rely on
their original host's autoindex to browse them. Starlette's own ``StaticFiles``
has no such fallback -- a directory with no ``index.html`` just 404s -- so this
subclass adds a minimal plain listing for that one case only.
"""

from __future__ import annotations

import os
import stat
from html import escape
from urllib.parse import quote

import anyio.to_thread
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import URL
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.types import Scope


class ListingStaticFiles(StaticFiles):
    """``StaticFiles`` that renders a sorted directory listing instead of a
    404 when a directory has neither a matching file nor an index.html."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            full_path, stat_result = await anyio.to_thread.run_sync(self.lookup_path, path)
            if stat_result is None or not stat.S_ISDIR(stat_result.st_mode):
                raise
            if not scope["path"].endswith("/"):
                # Match StaticFiles' own index.html behavior: directory URLs
                # always end in "/" so relative links in the listing resolve.
                url = URL(scope=scope)
                return RedirectResponse(url=url.replace(path=url.path + "/"))
            return self._render_listing(full_path, path)

    def _render_listing(self, dir_path: str, route_path: str) -> HTMLResponse:
        try:
            names = sorted(os.listdir(dir_path))
        except OSError:
            raise HTTPException(status_code=404) from None

        title = "/" if route_path == "." else f"/{route_path}/"
        items = [] if route_path == "." else ['<li><a href="../">../</a></li>']
        for name in names:
            if name.startswith("."):
                continue
            is_dir = os.path.isdir(os.path.join(dir_path, name))
            suffix = "/" if is_dir else ""
            items.append(f'<li><a href="{quote(name)}{suffix}">{escape(name)}{suffix}</a></li>')
        body = "\n".join(items) or "<li><em>(empty)</em></li>"
        html = (
            "<!DOCTYPE html><meta charset=\"utf-8\">"
            f"<title>Index of {escape(title)}</title>"
            f"<h1>Index of {escape(title)}</h1>"
            f"<ul>{body}</ul>"
        )
        return HTMLResponse(html)
