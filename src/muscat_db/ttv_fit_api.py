"""HTTP contracts for TTV fitting; validation and defaults stay in ttv_fit and web.

``options`` is an open mapping: ``selection``, ``planets_ephem`` and
``planet_letters`` are interpreted by ``_resolve_ttv_options``, which tells an
absent ``selection`` from an explicit null. Dump requests with
``exclude_unset=True`` so that difference survives.
"""

from typing import Any

from pydantic import Field

from muscat_db.photometry_api import ActionResponse, JobStatus, RequestModel, ResponseModel

__all__ = ["TtvActionResponse", "TtvJobStatus", "TtvStartRequest", "TtvCommandRequest",
           "TtvRunRequest", "TtvCommandResponse"]


class TtvStartRequest(RequestModel):
    target: str = ""
    options: dict[str, Any] | None = Field(
        default=None, description="Form options, passed through unchanged; the server resolves selection and inputs"
    )
    confirm_overwrite: str = Field(
        "", description="The confirm_token of a 409 run_exists answer, to overwrite that run"
    )


class TtvCommandRequest(RequestModel):
    target: str = ""
    options: dict[str, Any] | None = None


class TtvRunRequest(RequestModel):
    """Cancel or delete one run; a blank run_name is the target's unnamed run."""

    target: str = ""
    run_name: str = ""


class TtvActionResponse(ActionResponse):
    delete_failed: bool = False


class TtvJobStatus(JobStatus):
    error: str | None = Field(None, description="Set when the target name is invalid")


class TtvCommandResponse(ResponseModel):
    ok: bool
    command: str
