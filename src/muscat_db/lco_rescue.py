"""Bounded, durable recovery of opted-in LCO observations.

One committed reservation permits at most one external submission. A timeout
or crash after claiming is deliberately not retried: LCO has no idempotency
key. Ambiguous outcomes are reconciled by a persisted unique name, never reposted.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import os
import uuid
import logging
from urllib.parse import urlencode

from muscat_db import lco
from muscat_db.database import get_conn
from muscat_db.lco_autoschedule import replacement_window, rescue_policy, _timestamp

_RESUME_RETRY_S = 300.0
_MAX_RECONCILE_S = 86400.0
_RECONCILING = ("reserved", "uncertain")
logger = logging.getLogger(__name__)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _reason(path, request_id, reason):
    with get_conn(path) as conn:
        conn.execute(
            "UPDATE lco_observation_requests SET rescue_reason=? WHERE request_id=?",
            (reason, request_id),
        )
        conn.commit()


def _observations(request_id, user, token):
    result = lco._lco_api_request(
        f"https://observe.lco.global/api/requests/{int(request_id)}/observations/",
        user_name=user,
        token=token,
        require_own_token=True,
    )
    # Upstream returns a complete list, not a paginated archive response.
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise ValueError("Unrecognized observation history; recovery deferred")
    return result


def _claim(path, request_id, proposal, seconds, budget, payload, params, now):
    with get_conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute(
            "SELECT 1 FROM lco_observation_requests WHERE request_id=?", (request_id,)
        ).fetchone():
            return False
        if conn.execute(
            "SELECT 1 FROM lco_rescue_attempts WHERE original_id=?", (request_id,)
        ).fetchone():
            return False
        used = conn.execute(
            "SELECT COALESCE(SUM(reserved_seconds),0) FROM lco_rescue_attempts WHERE proposal=?",
            (proposal,),
        ).fetchone()[0]
        if float(used) + seconds > budget:
            conn.execute(
                "UPDATE lco_observation_requests SET rescue_reason=? WHERE request_id=?",
                ("not resubmitted: proposal rescue budget exhausted", request_id),
            )
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO lco_rescue_attempts(original_id,proposal,reserved_seconds,state,payload_json,params_json,created_at,updated_at,next_retry_at) VALUES(?,?,?,'reserved',?,?,?,?,?)",
            (
                request_id,
                proposal,
                seconds,
                _json(payload),
                _json(params),
                now,
                now,
                now + _RESUME_RETRY_S,
            ),
        )
        conn.execute(
            "UPDATE lco_observation_requests SET rescue_reason=? WHERE request_id=?",
            (
                "rescue reserved; interrupted attempts will be reconciled without another submission",
                request_id,
            ),
        )
        conn.commit()
        return True


def _register(path, request_id, payload, params, result, user, now):
    from muscat_db import lco_monitor

    rows = lco_monitor.record_submission(
        result,
        {**params, "requests": payload["requests"], "auto_resubmit": {}, "_requestgroup": payload},
        user,
        path=path,
        now=now,
    )
    ids = [row["request_id"] for row in rows]
    if len(ids) != 1:
        raise ValueError("Replacement response must contain exactly one child request")
    with get_conn(path) as conn:
        conn.execute(
            "UPDATE lco_observation_requests SET supersedes_request_id=? WHERE request_id=?",
            (request_id, ids[0]),
        )
        conn.execute(
            "UPDATE lco_observation_requests SET rescue_reason=? WHERE request_id=?",
            (f"auto-resubmitted as #{ids[0]}", request_id),
        )
        conn.execute(
            "UPDATE lco_rescue_attempts SET state=?, updated_at=? WHERE original_id=? AND state='accepted'",
            (
                "cleanup_pending" if params.get("_rescue_protocol") == 2 else "submitted",
                now,
                request_id,
            ),
        )
        conn.commit()


def _notify(path, request, event, text, now):
    """Persist a private notice and its deduplication key in one transaction."""
    owner = (request.get("user_name") or "").strip()
    if not owner:
        return
    try:
        with get_conn(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM lco_rescue_notices WHERE original_id=? AND event=?",
                (request["request_id"], event),
            ).fetchone():
                return
            cursor = conn.execute(
                "INSERT INTO chat_messages(user_name,text,kind,created_at,mentions,visible_to) "
                "VALUES('system',?,'system',?,'[]',?)",
                (f"LCO rescue #{request['request_id']}: {text}", now, owner),
            )
            message_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO lco_rescue_notices(original_id,event,message_id) VALUES(?,?,?)",
                (request["request_id"], event, message_id),
            )
            conn.commit()
        from muscat_db import chat

        chat.emit_persisted_system_message(
            {
                "id": message_id,
                "user": "system",
                "text": f"LCO rescue #{request['request_id']}: {text}",
                "kind": "system",
                "ts": now,
                "edited": False,
                "edited_at": None,
                "reactions": [],
                "mentions": [],
                "visible_to": owner,
                "room": None,
            },
            owner,
        )
    except Exception:
        logger.exception("Failed to deliver private rescue notice")


def _matches(expected, actual):
    """Match submitted fields, allowing response metadata and UTC formatting."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual
            and (
                _timestamp(value) == _timestamp(actual[key])
                if key in {"start", "end"}
                else _matches(value, actual[key])
            )
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(_matches(a, b) for a, b in zip(expected, actual))
        )
    return expected == actual


def _reconcile(payload, params, user, token):
    # Portal's name filter is icontains. Require exact name and payload, and
    # never follow pagination: an incomplete/ambiguous search is not evidence.
    if params.get("_rescue_protocol") != 2:
        raise ValueError("Legacy attempt needs manual reconciliation")
    query = urlencode({"proposal": payload["proposal"], "name": payload["name"], "limit": 100})
    page = lco._lco_api_request(
        f"https://observe.lco.global/api/requestgroups/?{query}",
        user_name=user,
        token=token,
        require_own_token=True,
    )
    if not isinstance(page, dict) or page.get("next") or not isinstance(page.get("results"), list):
        raise ValueError("Incomplete replacement search; manual reconciliation may be needed")
    matches = [
        r for r in page["results"] if isinstance(r, dict) and r.get("name") == payload["name"]
    ]
    if len(matches) != 1:
        raise ValueError("Replacement search is missing or ambiguous; no second submission")
    result = lco.get_requestgroup(
        matches[0]["id"], user_name=user, token=token, require_own_token=True
    )
    if not _matches(payload, result) or result.get("id") != matches[0]["id"]:
        raise ValueError("Replacement search payload differs; manual reconciliation required")
    return result


def _cleanup(request, *, path, now, user, token):
    request_id = int(request["request_id"])
    group = lco.get_requestgroup(
        request["requestgroup_id"],
        user_name=user,
        token=token,
        require_own_token=True,
    )
    children = group.get("requests") or []
    if len(children) != 1 or str(children[0].get("id")) != str(request_id):
        raise ValueError("Original membership changed; potential duplicate requires inspection")
    state = str(children[0].get("state") or group.get("state") or "").upper()
    group_state = str(group.get("state") or "").upper()
    if state == "COMPLETED" or group_state == "COMPLETED":
        _notify(
            path,
            request,
            "completed_original",
            "Original completed after replacement acceptance; potential duplicate. Inspect both requests.",
            now,
        )
    terminal = {"CANCELED", "COMPLETED", "WINDOW_EXPIRED", "FAILURE_LIMIT_REACHED"}
    if state not in terminal and group_state not in terminal:
        if state != "PENDING" or any(
            str(o.get("state")).upper() in {"PENDING", "IN_PROGRESS"}
            for o in _observations(request_id, user, token)
        ):
            raise ValueError("Original is scheduled or running; potential duplicate preserved")
        if os.environ.get("MUSCAT_LCO_ALLOW_SUBMIT") != "1":
            raise ValueError("Live submission disabled; potential duplicate preserved")
        canceled = lco._lco_api_request(
            f"https://observe.lco.global/api/requestgroups/{int(request['requestgroup_id'])}/cancel/",
            method="POST",
            user_name=user,
            token=token,
            require_own_token=True,
        )
        if str(canceled.get("state") or "").upper() != "CANCELED":
            raise ValueError("Original cancellation not confirmed; potential duplicate")
    with get_conn(path) as conn:
        conn.execute(
            "UPDATE lco_rescue_attempts SET state='submitted',updated_at=? WHERE original_id=? AND state='cleanup_pending'",
            (now, request_id),
        )
        conn.commit()


def _clip(kind, base, window, constraints):
    """Clip one window; only WindowNotObservable means confirmed invisibility."""
    params = {**base, "windows": [dict(window)]}
    lco._clip_windows_to_observability(
        kind,
        params,
        constraints.get("max_airmass", 1.6),
        constraints.get("min_lunar_distance", 30),
        constraints.get("max_lunar_phase", 1),
        strict=True,
    )
    if len(params.get("windows") or []) != 1:
        raise ValueError("Observability check returned an unexpected window set")
    return params


def resume_accepted(request, *, path, now, immediate=False):
    """Reconcile a single POST, register success, then safely retire the original."""
    request_id = int(request["request_id"])
    with get_conn(path) as conn:
        attempt = conn.execute(
            "SELECT state,payload_json,params_json,result_json,next_retry_at,retry_count FROM lco_rescue_attempts WHERE original_id=?",
            (request_id,),
        ).fetchone()
    if not attempt or attempt[0] not in {*_RECONCILING, "accepted", "cleanup_pending"}:
        return
    state, payload_json, params_json, result_json, next_retry_at, retry_count = attempt
    if not immediate and now < next_retry_at:
        return
    if state in _RECONCILING:
        retry_count += 1
        delay = min(_RESUME_RETRY_S * 2 ** min(retry_count, 32), _MAX_RECONCILE_S)
    else:
        delay = _RESUME_RETRY_S
    with get_conn(path) as conn:
        claimed = conn.execute(
            "UPDATE lco_rescue_attempts SET updated_at=?,next_retry_at=?,retry_count=? "
            "WHERE original_id=? AND state=? AND next_retry_at=?",
            (now, now + delay, retry_count, request_id, state, next_retry_at),
        ).rowcount
        conn.commit()
    if claimed != 1:
        return
    payload, params = json.loads(payload_json), json.loads(params_json)
    try:
        user = request.get("user_name")
        if not user:
            raise ValueError("A submitter with a saved personal token is required")
        if state in {"reserved", "uncertain"}:
            token = lco._get_lco_api_token(user, require_own_token=True)
            result = _reconcile(payload, params, user, token)
            with get_conn(path) as conn:
                advanced = conn.execute(
                    "UPDATE lco_rescue_attempts SET state='accepted',result_json=?,next_retry_at=? "
                    "WHERE original_id=? AND state IN ('reserved','uncertain')",
                    (_json(result), now + _RESUME_RETRY_S, request_id),
                ).rowcount
                conn.commit()
            if advanced != 1:
                return
            result_json, state = _json(result), "accepted"
        if state == "accepted":
            _register(path, request_id, payload, params, json.loads(result_json), user, now)
            if params.get("_rescue_protocol") != 2:
                return  # Legacy accepted submissions already canceled their original.
            state = "cleanup_pending"
        token = lco._get_lco_api_token(user, require_own_token=True)
        _cleanup(request, path=path, now=now, user=user, token=token)
        replacement_id = json.loads(result_json)["requests"][0]["id"]
        _reason(path, request_id, f"auto-resubmitted as #{replacement_id}")
        _notify(
            path,
            request,
            "submitted",
            f"Replacement #{replacement_id} is tracked; original cleanup is complete.",
            now,
        )
    except Exception as exc:
        if state == "accepted":
            reason = f"replacement accepted but registration failed; retrying next poll: {exc}"
        elif state == "cleanup_pending":
            reason = f"replacement tracked; original cleanup pending; potential duplicate: {exc}"
        else:
            reason = (
                f"uncertain rescue; reconciliation pending; no second submission; next check in "
                f"{round(delay / 60)} min: {exc}"
            )
        _reason(path, request_id, reason)
        _notify(path, request, state, reason, now)
        if state in _RECONCILING and delay >= _MAX_RECONCILE_S:
            _notify(
                path,
                request,
                "manual_review",
                f"Reconciliation of {payload.get('name')} is now checked daily; budget stays "
                "reserved. Inspect the LCO portal and resolve it manually.",
                now,
            )


def resume_terminal_accepted(*, path, now, limit):
    """Bounded restart recovery independent of the original's monitor state."""
    with get_conn(path, row_factory=None) as conn:
        cursor = conn.execute(
            "SELECT r.* FROM lco_rescue_attempts a JOIN lco_observation_requests r "
            "ON r.request_id=a.original_id WHERE a.state IN ('reserved','uncertain','accepted','cleanup_pending') "
            "AND a.next_retry_at<=? ORDER BY a.next_retry_at LIMIT ?",
            (now, limit),
        )
        columns = [description[0] for description in cursor.description]
        requests = [dict(zip(columns, row)) for row in cursor.fetchall()]
    for request in requests:
        resume_accepted(request, path=path, now=now)
    return len(requests)


def recover(request, group, child, frames, *, path, now):
    """Recover a dropped observation, without interrupting archive monitoring."""
    request_id = int(request["request_id"])
    params = json.loads(request.get("payload_json") or "{}")
    if not params.get("auto_resubmit") or request.get("supersedes_request_id"):
        return
    try:
        with get_conn(path) as conn:
            attempt = conn.execute(
                "SELECT 1 FROM lco_rescue_attempts WHERE original_id=?",
                (request_id,),
            ).fetchone()
        if attempt:
            return
        policy = rescue_policy(params["auto_resubmit"], request["proposal"])
        if policy is None:
            return
        if str(child.get("id")) != str(request_id):
            raise ValueError("Original request is absent from the live group")
        state = str(child.get("state") or group.get("state") or "").upper()
        if str(group.get("state") or "").upper() == "CANCELED" or state == "CANCELED":
            _reason(
                path,
                request_id,
                "not resubmitted: request canceled (including cancellations in the LCO portal)",
            )
            return
        if state == "COMPLETED":
            _reason(path, request_id, "not resubmitted: request completed")
            return
        if os.environ.get("MUSCAT_LCO_ALLOW_SUBMIT") != "1":
            _reason(path, request_id, "not resubmitted: live submission disabled")
            return
        user = request.get("user_name")
        if not user:
            raise ValueError("A submitter with a saved personal token is required")
        token = lco._get_lco_api_token(user, require_own_token=True)
        history = _observations(request_id, user, token)
        if any(str(o.get("state")).upper() in {"PENDING", "IN_PROGRESS"} for o in history):
            _reason(
                path,
                request_id,
                "not resubmitted: LCO already has a scheduled or running observation",
            )
            return
        dropped = [
            o
            for o in history
            if str(o.get("state")).upper() in {"ABORTED", "FAILED"}
            or (
                str(o.get("state")).upper() == "NOT_ATTEMPTED"
                and o.get("end")
                and _timestamp(o["end"]).timestamp() <= now
            )
        ]
        if not dropped and state not in {"WINDOW_EXPIRED", "FAILURE_LIMIT_REACHED"}:
            return
        payload = copy.deepcopy(params.get("_requestgroup") or {})
        if len(payload.get("requests") or []) != 1 or len(group.get("requests") or []) != 1:
            raise ValueError(
                "Recovery requires a single-request group; other requests must not be canceled"
            )
        replacement = payload["requests"][0]
        configurations = replacement.get("configurations") or []
        if len(configurations) != 1 or configurations[0].get("type") != "REPEAT_EXPOSE":
            raise ValueError("Recovery requires one REPEAT_EXPOSE configuration")
        original = copy.deepcopy(replacement["windows"])
        observed_until = None
        with get_conn(path) as conn:
            saved_frames = conn.execute(
                "SELECT metadata_json FROM lco_observation_frames WHERE request_id=? AND state IN ('downloaded','ingested')",
                (request_id,),
            ).fetchall()
        frames = list(frames) + [json.loads(row[0]) for row in saved_frames]
        for frame in frames:
            stamp = frame.get("observation_date")
            exposure = frame.get("exposure_time")
            if not stamp or exposure is None:
                raise ValueError("Frame exposure timing unavailable; cannot exclude observed time")
            duration = float(exposure)
            if not math.isfinite(duration) or duration < 0:
                raise ValueError("Invalid frame exposure duration")
            end = _timestamp(stamp) + dt.timedelta(seconds=duration)
            observed_until = max(observed_until, end) if observed_until else end
        # Complete configuration summaries can precede archive ingestion.
        for observation in history:
            for config in observation.get("configuration_statuses") or []:
                end = (config.get("summary") or {}).get("end")
                if end:
                    end = _timestamp(end)
                    observed_until = max(observed_until, end) if observed_until else end
        now_iso = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()
        selected = replacement_window(
            original,
            now=now_iso,
            observed_until=observed_until.isoformat() if observed_until else None,
            min_fraction=policy["min_fraction"],
            min_minutes=policy["min_minutes"],
        )
        if not selected:
            _reason(path, request_id, "not resubmitted: no future window meets every threshold")
            return
        cfg = configurations[0]
        site = replacement.get("location", {}).get("site")
        if not site:
            raise ValueError(
                "An explicitly pinned site is required to establish original viability"
            )
        if int(replacement.get("configuration_repeats", 1)) != 1:
            raise ValueError("Repeated configuration cycles are not supported for rescue")
        target, constraints = cfg["target"], cfg.get("constraints") or {}
        base = {
            "site": site,
            "type": "REPEAT_EXPOSE",
            "ra": target["ra"],
            "dec": target["dec"],
            "twilight": params.get("twilight"),
        }
        remaining = [
            {"start": max(_timestamp(w["start"]), _timestamp(now_iso)).isoformat(), "end": w["end"]}
            for w in original
            if _timestamp(w["end"]).timestamp() > now
        ]
        observable = []
        for window in remaining:
            try:
                observable.append(_clip(params.get("kind"), base, window, constraints)["windows"][0])
            except lco.WindowNotObservable:
                continue
        original_seconds = float(cfg["repeat_duration"]) + lco._REPEAT_EXPOSE_SETUP_OVERHEAD_S
        if (
            not math.isfinite(original_seconds)
            or original_seconds <= lco._REPEAT_EXPOSE_SETUP_OVERHEAD_S
        ):
            raise ValueError("Invalid original duration")
        if state == "PENDING" and any(
            (_timestamp(w["end"]) - _timestamp(w["start"])).total_seconds() >= original_seconds
            for w in observable
        ):
            _reason(
                path,
                request_id,
                "not resubmitted: original still fits a remaining observable window",
            )
            return
        if not observable:
            _reason(path, request_id, "not resubmitted: no remaining window is observable")
            return
        selected = replacement_window(
            original,
            now=now_iso,
            observable_windows=observable,
            observed_until=observed_until.isoformat() if observed_until else None,
            min_fraction=policy["min_fraction"],
            min_minutes=policy["min_minutes"],
        )
        if not selected:
            _reason(path, request_id, "not resubmitted: observable remainder below thresholds")
            return
        clipped = _clip(params.get("kind"), base, selected, constraints)
        seconds = (_timestamp(selected["end"]) - _timestamp(selected["start"])).total_seconds()
        if seconds < lco._REPEAT_EXPOSE_SETUP_OVERHEAD_S + 60:
            raise ValueError("Remainder too short for setup and one minute of observing")
        clipped["windows"] = [selected]
        replacement["windows"] = [selected]
        cfg["repeat_duration"] = min(float(cfg["repeat_duration"]), lco._repeat_duration(clipped))
        payload["name"] = (
            str(payload.get("name") or request.get("name") or "request")[:12]
            + "_auto_"
            + uuid.uuid4().hex
        )
        ipp = lco.max_allowable_ipp(payload, user, token=token)
        if not isinstance(ipp, dict) or not ipp or ipp.get("errors"):
            raise ValueError("Replacement failed the LCO IPP dry-run")
        limits = []

        def ipp_limits(node):
            if isinstance(node, dict):
                if "max_allowable_ipp_value" in node:
                    limit = float(node["max_allowable_ipp_value"])
                    if not math.isfinite(limit):
                        raise ValueError("Invalid IPP limit")
                    limits.append(limit)
                for value in node.values():
                    ipp_limits(value)
            elif isinstance(node, list):
                for value in node:
                    ipp_limits(value)

        ipp_limits(ipp)
        if limits and float(payload.get("ipp_value", 1)) > min(limits):
            raise ValueError("Requested IPP exceeds the replacement's available IPP")
        # Re-read before reserving: manual cancellation/completion during the
        # dry-run must not be overridden by an automatic replacement.
        fresh = lco.get_requestgroup(
            request["requestgroup_id"], user_name=user, token=token, require_own_token=True
        )
        fresh_children = fresh.get("requests") or []
        fresh_state = str(fresh.get("state") or "").upper()
        if (
            fresh_state not in {"PENDING", "WINDOW_EXPIRED", "FAILURE_LIMIT_REACHED"}
            or len(fresh_children) != 1
            or str(fresh_children[0].get("id")) != str(request_id)
            or str(fresh_children[0].get("state") or "").upper() in {"CANCELED", "COMPLETED"}
        ):
            _reason(path, request_id, "not resubmitted: original changed during validation")
            return
        if any(
            str(o.get("state")).upper() in {"PENDING", "IN_PROGRESS"}
            for o in _observations(request_id, user, token)
        ):
            _reason(
                path, request_id, "not resubmitted: LCO scheduled an observation during validation"
            )
            return
        if os.environ.get("MUSCAT_LCO_ALLOW_SUBMIT") != "1":
            raise ValueError("Live submission disabled during validation")
        replacement_params = {
            **params,
            "auto_resubmit": {},
            "_rescue_protocol": 2,
            "name": payload["name"],
            "windows": [selected],
        }
        if not _claim(
            path,
            request_id,
            request["proposal"],
            seconds,
            policy["budget_seconds"],
            payload,
            replacement_params,
            now,
        ):
            return
        try:
            result = lco.submit_requestgroup(payload, user, token=token)
            with get_conn(path) as conn:
                conn.execute(
                    "UPDATE lco_rescue_attempts SET state='accepted',result_json=?,updated_at=?,next_retry_at=? "
                    "WHERE original_id=? AND state IN ('reserved','uncertain')",
                    (_json(result), now, now + _RESUME_RETRY_S, request_id),
                )
                conn.commit()
        except Exception as exc:
            # Only definite validation/auth rejection releases allocation. Timeouts
            # (including HTTP 408), 5xx and malformed successes remain uncertain.
            rejected = isinstance(exc, lco.LcoError) and exc.status in {
                400,
                401,
                403,
                404,
                405,
                422,
            }
            attempt_state = "rejected" if rejected else "uncertain"
            reason = (
                "rescue rejected; original preserved; budget released"
                if rejected
                else "rescue uncertain; original preserved; reconciliation pending; no second submission"
            )
            with get_conn(path) as conn:
                changed = conn.execute(
                    "UPDATE lco_rescue_attempts SET state=?,reserved_seconds=CASE WHEN ? THEN 0 ELSE reserved_seconds END,"
                    "reason=?,updated_at=?,next_retry_at=? WHERE original_id=? AND state='reserved'",
                    (attempt_state, rejected, reason, now, now + _RESUME_RETRY_S, request_id),
                ).rowcount
                conn.commit()
            if changed != 1:
                return
            _reason(path, request_id, reason)
            _notify(path, request, attempt_state, reason, now)
            return
        resume_accepted(request, path=path, now=now, immediate=True)
    except Exception as exc:
        _reason(path, request_id, f"not resubmitted: {exc}")


def proposal_usage(path):
    with get_conn(path) as conn:
        rows = conn.execute(
            "SELECT proposal,COUNT(*),SUM(CASE WHEN state IN ('accepted','cleanup_pending','submitted') THEN 1 ELSE 0 END),SUM(reserved_seconds) FROM lco_rescue_attempts GROUP BY proposal"
        ).fetchall()
    return [
        {
            "proposal": r[0],
            "attempts": r[1],
            "submitted": r[2],
            "reserved_minutes": round(r[3] / 60, 6),
        }
        for r in rows
    ]
