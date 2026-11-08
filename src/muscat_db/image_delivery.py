"""Display-only derivatives and private validation for authorized image files.

Callers must resolve and authorize the source before entering this module. Cache
files are never mounted publicly; original pipeline products are never modified.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import io
import os
from pathlib import Path
import tempfile
import threading

from fastapi import HTTPException, Request
from PIL import Image, UnidentifiedImageError
from starlette.responses import FileResponse, Response

WIDTHS = (480, 960, 1560)
_IMAGE_TYPES = {'.png': 'image/png', '.gif': 'image/gif'}
_CACHE_LIMIT = 256 * 1024 * 1024
_ENCODE_LOCK = threading.Lock()


def _stamp(path: Path) -> tuple[int, int, int]:
    st = path.stat()
    return st.st_mtime_ns, st.st_ctime_ns, st.st_size


@lru_cache(maxsize=1024)
def _dimensions(path: Path, stamp: tuple[int, int, int]) -> tuple[int, int]:
    with Image.open(path) as im:
        return im.size


def metadata(path: Path) -> dict:
    """Read headers only; a partially written product must not break the page."""
    try:
        stamp = _stamp(path)
        width, height = _dimensions(path, stamp)
        return {'width': width, 'height': height, 'version': str(stamp[0]),
                'variants': [(w, min(w, width)) for i, w in enumerate(WIDTHS)
                             if i == 0 or width > WIDTHS[i - 1]]}
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return {}


def _preview(path: Path, width: int, stamp: tuple[int, int, int]) -> bytes:
    root = Path(os.environ.get('MUSCAT_TMPDIR', str(Path.home() / 'temp'))).expanduser() / 'image-previews'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(f'png-v1:{path.resolve()}:{stamp}:{width}'.encode()).hexdigest()
    dest = root / (key + '.png')
    # Bound decoding to one image per web process. The file lock also coalesces
    # generation across workers sharing this host-local cache.
    import fcntl
    with _ENCODE_LOCK, (root / '.encode.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if dest.exists():
                return dest.read_bytes()
            with Image.open(path) as im:
                im.seek(0)  # GIF previews are static posters.
                im.thumbnail((width, max(1, round(im.height * min(1, width / im.width)))), Image.Resampling.LANCZOS)
                # Preserve alpha; never quantize scientific plot colours.
                im = im.convert('RGBA' if 'transparency' in im.info or 'A' in im.getbands() else 'RGB')
                buf = io.BytesIO()
                im.save(buf, format='PNG', compress_level=3)
                data = buf.getvalue()
            if _stamp(path) != stamp:
                raise HTTPException(409, 'Image changed while generating preview; retry')
            # Atomic publication, with bounded on-disk storage. Responses hold
            # their own bytes, so pruning cannot break an in-flight download.
            cached = sorted(root.glob('*.png'), key=lambda p: p.stat().st_mtime_ns)
            total = sum(p.stat().st_size for p in cached)
            for old in cached:
                if total + len(data) <= _CACHE_LIMIT:
                    break
                total -= old.stat().st_size
                old.unlink(missing_ok=True)
            if len(data) <= _CACHE_LIMIT:
                with tempfile.NamedTemporaryFile(dir=root, suffix='.part', delete=False) as tmp:
                    temp = Path(tmp.name)
                    try:
                        tmp.write(data)
                        tmp.flush()
                        os.replace(temp, dest)
                    finally:
                        temp.unlink(missing_ok=True)
            return data
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def image_response(path: Path, request: Request, preview_width: int | None = None) -> Response:
    """Revalidate on every visit, including old ?v= URLs and changed permissions."""
    if preview_width is not None and preview_width not in WIDTHS:
        raise HTTPException(422, 'Unsupported preview width')
    media_type = _IMAGE_TYPES.get(path.suffix.lower())
    if media_type is None:
        if preview_width is not None:
            raise HTTPException(415, 'Previews require a PNG or GIF')
        return FileResponse(path, headers={'Cache-Control': 'no-store'})
    try:
        stamp = _stamp(path)
        digest = hashlib.sha256(f'{path.resolve()}:{stamp}:{preview_width}:png-v1'.encode()).hexdigest()
        etag = '"' + digest + '"'
        headers = {'Cache-Control': 'private, no-cache', 'ETag': etag,
                   # Avoid recompressing PNG/GIF in the global gzip middleware.
                   'Content-Encoding': 'identity'}
        validators = request.headers.get('if-none-match', '').split(',')
        if any(v.strip().removeprefix('W/') in (etag, '*') for v in validators):
            return Response(status_code=304, headers=headers)
        if preview_width is None:
            return FileResponse(path, media_type=media_type, headers=headers)
        data = _preview(path, preview_width, stamp)
        return Response(data, media_type='image/png', headers=headers)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise HTTPException(404, 'Image unavailable') from exc
