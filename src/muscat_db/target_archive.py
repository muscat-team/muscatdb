"""Target archive inventory, independent of observations reported to ExoFOP."""
from __future__ import annotations

from collections import OrderedDict
import datetime as dt
import hashlib
from pathlib import Path
import threading
import time

from muscat_db import catalog, exofop, lco
from muscat_db.access import is_denied
from muscat_db.coord import CoordRepr, unpack
from muscat_db.database import get_conn, sql_not_denied
from muscat_db.dayobs import dayobs_from_filename

_CACHE_TTL = 3600
_CACHE_SIZE = 16
_cache: OrderedDict = OrderedDict()
# Protect cache operations; matching searches share an in-flight event.
_cache_lock = threading.Lock()
_inflight: dict[tuple, threading.Event] = {}


def _night(frame: dict) -> str:
    night = dayobs_from_filename(str(frame.get('filename') or frame.get('basename') or ''))
    if night:
        return '20' + night[:2] + '-' + night[2:4] + '-' + night[4:6]
    raw = str(frame.get('DAY_OBS') or '').replace('-', '')
    if len(raw) == 8 and raw.isdigit():
        return raw[:4] + '-' + raw[4:6] + '-' + raw[6:]
    return lco._lco_observing_date(frame)


def _stem(filename: str) -> str:
    return Path(filename).name.removesuffix('.fz').removesuffix('.fits')


def _coords(target: str, denied: frozenset[str]) -> tuple[float, float] | None:
    # Supply only visible header centres for the last-resort coordinate fallback.
    clause, params = sql_not_denied(denied)
    with get_conn() as conn:
        conn.create_aggregate('coord_repr', 2, CoordRepr)
        row = conn.execute(
            f'SELECT coord_repr(ra, declination) FROM frames WHERE object = ? AND {clause}',
            (target, *params),
        ).fetchone()
    ra, dec = unpack(row[0])
    coords = catalog._target_crossmatch_coords(target, [{'ra': ra, 'dec': dec}])
    return coords[0] if coords else None


def _search(coords, user_name, start, end, *, require_own_token):
    token = lco._get_lco_api_token(user_name, require_own_token=require_own_token)
    key = (coords, user_name, hashlib.sha256(token.encode()).hexdigest(), start, end)
    while True:
        with _cache_lock:
            now = time.monotonic()
            for old in list(_cache):
                if now - _cache[old][0] >= _CACHE_TTL:
                    del _cache[old]
            if key in _cache:
                _cache.move_to_end(key)
                return _cache[key][1]
            waiting = _inflight.get(key)
            if waiting is None:
                _inflight[key] = threading.Event()
                break
        waiting.wait(timeout=1)
        yield {'progress': 'Waiting for another archive search…'}
    try:
        filters = {'covers': f'POINT({coords[0]} {coords[1]})', 'reduction_level': '91',
                   'configuration_type': 'EXPOSE', 'limit': '1000'}
        if start:
            filters['start'] = start
        if end:
            # User-facing end date is inclusive; archive timestamps use an upper bound.
            filters['end'] = (dt.date.fromisoformat(end) + dt.timedelta(days=1)).isoformat()
        # Discard download URLs and other large payload fields as each page arrives.
        fields = ('id', 'filename', 'basename', 'SITEID', 'TELID', 'INSTRUME', 'DAY_OBS',
                  'DATE_OBS', 'observation_date', 'OBJECT', 'FILTER', 'EXPTIME',
                  'proposal_id', 'request_id', 'public_date')
        aliases = {'SITEID': 'site_id', 'TELID': 'telescope_id', 'INSTRUME': 'instrument_id',
                   'DAY_OBS': 'observation_day', 'OBJECT': 'target_name',
                   'FILTER': 'primary_optical_element', 'EXPTIME': 'exposure_time'}
        result = {'results': [], 'truncated': False}
        try:
            for page in lco.archive_cursor_pages(filters, user_name, token=token):
                for frame in page:
                    compact = {k: frame[k] for k in fields if k in frame}
                    for field, alias in aliases.items():
                        if compact.get(field) is None and alias in frame:
                            compact[field] = frame[alias]
                    result['results'].append(compact)
                # Don't expose raw counts before proposal filtering.
                yield {'progress': 'Searching all archive nights…'}
        except lco.ArchiveSearchLimit:
            result['truncated'] = True
        if not result['truncated']:
            with _cache_lock:
                _cache[key] = (time.monotonic(), result)
                while len(_cache) > _CACHE_SIZE:
                    _cache.popitem(last=False)
        return result
    finally:
        with _cache_lock:
            _inflight.pop(key).set()


def inventory_events(target: str, user_name: str | None, *, denied=frozenset(), start='', end=''):
    """Return visible requests, with fresh local presence and cached archive metadata."""
    if not target.strip():
        raise lco.LcoError('Enter a target name.', status=400)
    try:
        for value in (start, end):
            if value:
                if dt.date.fromisoformat(value).isoformat() != value:
                    raise ValueError
        if start and end and start > end:
            raise ValueError
        if end == '9999-12-31':
            raise ValueError
    except ValueError:
        raise lco.LcoError('Use valid dates with start on or before end.', status=400)
    coords = _coords(target, denied)
    if coords is None:
        raise lco.LcoError(f'Could not resolve coordinates for {target!r}.', status=422)
    yield {"progress": "Resolving target and searching archive…"}
    result = yield from _search(coords, user_name, start, end, require_own_token=bool(denied))
    groups = {}
    now = dt.datetime.now(dt.timezone.utc)
    for frame in result['results']:
        if lco.is_engineering_object(frame.get('OBJECT') or ''):
            continue
        night = _night(frame)
        try:
            instrument = lco.infer_archive_instrument(frame)
        except lco.LcoError:
            instrument = str(frame.get('INSTRUME') or 'unknown')
        proposal = str(frame.get('proposal_id') or '')
        if is_denied(proposal, denied, instrument=instrument,
                     obsdate=night.replace('-', '')[2:], obj=frame.get('OBJECT')):
            continue
        site = str(frame.get('SITEID') or '')
        telescope = str(frame.get('TELID') or '')
        request = str(frame.get('request_id') or '')
        key = (night, instrument, request, site, telescope, proposal)
        group = groups.setdefault(key, dict(date=night, instrument=instrument, request_id=request,
            site=site, telescope=telescope, proposal=proposal, filters=set(), exposures=set(), objects=set(),
            filenames=set(), frame_count=0, proprietary=False))
        # Archive pagination can overlap; count each physical frame once.
        filename = _stem(str(frame.get('filename') or frame.get('basename') or ''))
        identity = filename or str(frame.get('id') or '')
        if identity and identity in group['filenames']:
            continue
        group['filenames'].add(identity)
        group['frame_count'] += 1
        object_name = str(frame.get('OBJECT') or '').strip()
        if object_name:
            group['objects'].add(object_name)
        group['filters'].add(str(frame.get('FILTER') or ''))
        if frame.get('EXPTIME') is not None:
            group['exposures'].add(str(frame['EXPTIME']))
        try:
            release = dt.datetime.fromisoformat(str(frame.get('public_date') or '').replace('Z', '+00:00'))
            group['proprietary'] |= release.replace(tzinfo=release.tzinfo or dt.timezone.utc) > now
        except ValueError:
            pass

    # Query indexed instrument/night pairs once, not the whole frames table per request.
    local = {}
    clause, params = sql_not_denied(denied)
    with get_conn() as conn:
        for group in groups.values():
            pair = (group['instrument'], group['date'].replace('-', '')[2:])
            if pair not in local:
                local[pair] = {_stem(r[0]): r[1] for r in conn.execute(
                    f'SELECT filename, object FROM frames WHERE instrument = ? AND obsdate = ? AND {clause}',
                    (*pair, *params),
                )}
    try:
        report = exofop.build_time_series_report(target, denied=denied)
    except Exception:
        report = {'ok': False}
    entries = report.get('time_series') or []
    rows = []
    for group in groups.values():
        pair = (group['instrument'], group['date'].replace('-', '')[2:])
        matched = sorted(group.pop('filenames') & local[pair].keys())
        group['local_frames'] = len(matched)
        group['local_target'] = next((local[pair][name] for name in matched if local[pair][name]), target)
        group['exists'] = group['local_frames'] == group['frame_count']
        group['objects'] = sorted(group['objects'])
        group['filters'] = sorted(group['filters'])
        group['exposures'] = sorted(group['exposures'])
        group['reported'] = any(
            e.get('lco') and e.get('instrument') == group['instrument']
            and (not e.get('site') or e['site'] == group['site'])
            and pair[1] in exofop._obsdate_window(str(e.get('tsdate') or ''))
            for e in entries
        ) if report.get('ok') else None
        rows.append(group)
    yield {'ok': True, 'truncated': result['truncated'],
           'rows': sorted(rows, key=lambda r: (r['date'], r['request_id']), reverse=True)}


def inventory(target: str, user_name: str | None, **kwargs):
    """Non-streaming API for callers that only need the completed inventory."""
    for event in inventory_events(target, user_name, **kwargs):
        if 'ok' in event:
            return event
