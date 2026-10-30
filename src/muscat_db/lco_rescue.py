"""Bounded, durable recovery of opted-in LCO observations.

One committed reservation permits at most one external submission. A timeout
or crash after claiming is deliberately not retried: LCO has no idempotency
key, so an operator must reconcile ambiguous outcomes in the observing portal.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import os

from muscat_db import lco
from muscat_db.database import get_conn
from muscat_db.lco_autoschedule import replacement_window, rescue_policy, _timestamp


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
            "INSERT INTO lco_rescue_attempts(original_id,proposal,reserved_seconds,state,payload_json,params_json,created_at,updated_at) VALUES(?,?,?,'reserved',?,?,?,?)",
            (request_id, proposal, seconds, _json(payload), _json(params), now, now),
        )
        conn.execute(
            "UPDATE lco_observation_requests SET rescue_reason=? WHERE request_id=?",
            ("rescue reserved; interrupted attempts require manual reconciliation", request_id),
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
            "UPDATE lco_rescue_attempts SET state='submitted', updated_at=? WHERE original_id=?",
            (now, request_id),
        )
        conn.commit()


def resume_accepted(request, *, path, now):
    """Register a saved success even when archive monitoring can finish early."""
    request_id = int(request["request_id"])
    with get_conn(path) as conn:
        attempt = conn.execute(
            "SELECT payload_json,params_json,result_json FROM lco_rescue_attempts WHERE original_id=? AND state='accepted'",
            (request_id,),
        ).fetchone()
    if not attempt:
        return
    try:
        _register(
            path,
            request_id,
            json.loads(attempt[0]),
            json.loads(attempt[1]),
            json.loads(attempt[2]),
            request.get("user_name"),
            now,
        )
    except Exception as exc:
        _reason(
            path,
            request_id,
            f"replacement accepted but registration failed; retrying next poll: {exc}",
        )


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
            if str(o.get("state")).upper() in {"ABORTED", "FAILED", "NOT_ATTEMPTED", "CANCELED"}
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
            sites = {o.get("site") for o in dropped if o.get("site")}
            if len(sites) != 1:
                raise ValueError("A unique recovery site is required for observability checking")
            site = sites.pop()
            replacement.setdefault("location", {})["site"] = site
        target, constraints = cfg["target"], cfg.get("constraints") or {}
        clipped = {
            "windows": [selected],
            "site": site,
            "type": "REPEAT_EXPOSE",
            "ra": target["ra"],
            "dec": target["dec"],
            "twilight": params.get("twilight"),
        }
        lco._clip_windows_to_observability(
            params.get("kind"),
            clipped,
            constraints.get("max_airmass", 1.6),
            constraints.get("min_lunar_distance", 30),
            constraints.get("max_lunar_phase", 1),
            strict=True,
        )
        selected = replacement_window(
            original,
            now=now_iso,
            observable_windows=clipped["windows"],
            observed_until=observed_until.isoformat() if observed_until else None,
            min_fraction=policy["min_fraction"],
            min_minutes=policy["min_minutes"],
        )
        if not selected:
            _reason(path, request_id, "not resubmitted: observable remainder below thresholds")
            return
        seconds = (_timestamp(selected["end"]) - _timestamp(selected["start"])).total_seconds()
        if seconds < lco._REPEAT_EXPOSE_SETUP_OVERHEAD_S + 60:
            raise ValueError("Remainder too short for setup and one minute of observing")
        clipped["windows"] = [selected]
        replacement["windows"] = [selected]
        cfg["repeat_duration"] = lco._repeat_duration(clipped)
        payload["name"] = (
            str(payload.get("name") or request.get("name") or "request")[:45] + "_auto"
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
            if str(fresh.get("state") or "").upper() == "PENDING":
                canceled = lco._lco_api_request(
                    f"https://observe.lco.global/api/requestgroups/{int(request['requestgroup_id'])}/cancel/",
                    method="POST",
                    user_name=user,
                    token=token,
                    require_own_token=True,
                )
                if str(canceled.get("state") or "").upper() != "CANCELED":
                    raise ValueError("Superseded original cancellation was not confirmed")
            result = lco.submit_requestgroup(payload, user, token=token)
            with get_conn(path) as conn:
                conn.execute(
                    "UPDATE lco_rescue_attempts SET state='accepted',result_json=?,updated_at=? WHERE original_id=?",
                    (_json(result), now, request_id),
                )
                conn.commit()
            _register(path, request_id, payload, replacement_params, result, user, now)
        except Exception as exc:
            _reason(
                path, request_id, f"rescue outcome requires manual reconciliation; no retry: {exc}"
            )
    except Exception as exc:
        _reason(path, request_id, f"not resubmitted: {exc}")


def proposal_usage(path):
    with get_conn(path) as conn:
        rows = conn.execute(
            "SELECT proposal,COUNT(*),SUM(CASE WHEN state IN ('accepted','submitted') THEN 1 ELSE 0 END),SUM(reserved_seconds) FROM lco_rescue_attempts GROUP BY proposal"
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
