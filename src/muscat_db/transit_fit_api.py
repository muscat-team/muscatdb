"""HTTP contracts for transit fitting; validation and defaults stay in transit_fit.

``options`` is deliberately an open mapping. The form generates per-planet keys
(``period_b``, ``t0_c``, ``ror_prior_c``, ...) and the engine compares flags to
the string ``"true"``, so a fixed field list or value coercion would silently
drop or change what the fit sees. Dump requests with ``exclude_unset=True``:
absence is meaningful (``selected_csvs`` omitted means all light curves).
"""

from typing import Any, Literal

from pydantic import Field

from muscat_db.photometry_api import ActionResponse, DatasetRequest, ResponseModel


class FitRunRequest(DatasetRequest):
    test_run: bool = False
    options: dict[str, Any] | None = Field(
        default=None, description="Form options, passed through unchanged (planet keys carry a letter suffix)"
    )
    selected_csvs: list[Any] | None = Field(
        default=None, description="Light curves to fit; omit for all, [] selects none"
    )
    confirm_overwrite: str = Field(
        "", description="The confirm_token of a 409 run_exists answer, to overwrite that run"
    )


class FitLogpRequest(DatasetRequest):
    options: dict[str, Any] | None = None
    selected_csvs: list[Any] | None = None


class FitCancelRequest(DatasetRequest):
    run_id: str = ""
    run: str = Field("", description="Legacy alias; run_id takes precedence")


class FitDeleteRequest(DatasetRequest):
    run_id: str = ""


class FitActionResponse(ActionResponse):
    delete_failed: bool = False


class FitLogpResponse(ResponseModel):
    ok: bool
    error: str | None = None
    logp: str | None = None
    text: str | None = None
    detail: str | None = None


class FitJobStatus(ResponseModel):
    state: Literal["none", "pending", "running", "cancelling", "finalizing", "done", "error", "cancelled"]
    log: str
    returncode: int | None
    elapsed: int
    error: str | None = Field(None, description="Set when the run identifiers are invalid")
    error_desc: str = ""
    run_type: Literal["test", "full"] | None = None


class FitPlot(ResponseModel):
    file: str
    created_at: str
    version: str


class FitSummary(ResponseModel):
    headers: list[str]
    rows: list[dict[str, str]]


class FitOutputs(ResponseModel):
    """Filenames are fetched through the /file routes."""

    has_any: bool
    plots: list[FitPlot]
    systematics_plots: list[FitPlot]
    summary: FitSummary | None
    detection_snr: Any = None
    has_log: bool
    has_fit_yaml: bool
    has_sys_yaml: bool
    has_meta_yaml: bool
    extra_files: list[str]


class FitOutputsResponse(ResponseModel):
    ok: bool
    outputs: FitOutputs
