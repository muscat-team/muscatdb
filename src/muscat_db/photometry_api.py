"""HTTP contracts for photometry; pipeline defaults and validation stay in photometry.

Dump requests with ``exclude_unset=True``: absence carries meaning in the legacy
option normalizer (notably bands, overwrite, and avoid_nearby_stars). Numeric
inputs retain the form's blank-string sentinel until that normalizer runs.
"""

from typing import Annotated, Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SkipValidation

from muscat_db import photometry as phot


def _form_number(value):
    if isinstance(value, bool):
        raise ValueError("a boolean is not a numeric option")
    return "" if isinstance(value, str) and not value.strip() else value


def _form_text(value):
    """Keep accepting a bare number where the pipeline takes comma-separated text."""
    if isinstance(value, bool):
        raise ValueError("a boolean is not a text option")
    return str(value) if isinstance(value, (int, float)) else value


FormText = Annotated[str, BeforeValidator(_form_text)]
FormInt = Annotated[int | Literal[""] | None, BeforeValidator(_form_number)]
FormFloat = Annotated[float | Literal[""] | None, BeforeValidator(_form_number)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PhotometryOptions(RequestModel):
    """Run options. Blank numeric fields keep pipeline defaults.

    Comma-separated list options remain strings so validate_run_options retains
    its existing syntax checks. Post-process settings are saved by the same GUI
    form but do not become reduction command arguments.
    """

    run_name: str = ""
    bands: list[str] | str | None = phot.RUN_DEFAULTS["bands"]
    ref_band: str = ""
    refid: FormInt = ""
    ref_select: str = phot.RUN_DEFAULTS["ref_select"]
    ref_time: FormText = Field("", description="HH:MM[:SS] UT, ISO UTC, or Julian date")
    ref_select_top_k: FormInt = phot.RUN_DEFAULTS["ref_select_top_k"]
    aper_unit: str = phot.RUN_DEFAULTS["aper_unit"]
    aper_radii: str = Field("", description="MIN,MAX,DR; requires annulus")
    annulus: str = Field("", description="RIN,ROUT; requires aper_radii")
    target_id: Annotated[str | int, BeforeValidator(_form_number)] = ""
    comparison_ids: FormText = Field("", description="Comma-separated integer IDs")
    avoid_comparison_ids: FormText = Field("", description="Comma-separated integer IDs; requires ref_band")
    avoid_nearby_star_mode: str = phot.RUN_DEFAULTS["avoid_nearby_star_mode"]
    avoid_nearby_star: FormFloat = ""
    avoid_nearby_stars: bool = Field(True, description="Legacy flag used when mode is absent")
    target_coord: str = Field("", description="RA Dec; blank resolves the target")
    make_gif: bool = phot.RUN_DEFAULTS["make_gif"]
    plot_gaia_sources: bool = phot.RUN_DEFAULTS["plot_gaia_sources"]
    use_barycorrpy: bool = phot.RUN_DEFAULTS["use_barycorrpy"]
    test_run_frames: FormInt = phot.RUN_DEFAULTS["test_run_frames"]
    min_star_separation: FormFloat = phot.RUN_DEFAULTS["min_star_separation"]
    max_num_stars: FormInt = phot.RUN_DEFAULTS["max_num_stars"]
    n_stars_align: FormInt = ""
    cutout_size: FormInt = phot.RUN_DEFAULTS["cutout_size"]
    display_stack_nframes: FormInt = phot.RUN_DEFAULTS["display_stack_nframes"]
    ccd_trim: str = Field("", description="Y,X integer pixel trim")
    edge_margin: FormInt = ""
    bin_size_minutes: FormFloat = phot.RUN_DEFAULTS["bin_size_minutes"]
    nan_imputation_method: str = phot.RUN_DEFAULTS["nan_imputation_method"]
    overwrite: bool = phot.RUN_DEFAULTS["overwrite"]
    gif_stride: FormInt = phot.RUN_DEFAULTS["gif_stride"]
    sig_bkg: FormFloat = None
    sig_fwhm: FormFloat = None
    sig_dx: FormFloat = None
    sig_dy: FormFloat = None
    sig_flux: FormFloat = None
    flux_poly_deg: FormInt = phot.RUN_DEFAULTS["flux_poly_deg"]
    min_star_area: FormInt = phot.RUN_DEFAULTS["min_star_area"]
    wcs_method: str = phot.RUN_DEFAULTS["wcs_method"]
    centroid_method: str = phot.RUN_DEFAULTS["centroid_method"]
    cmap: str = phot.RUN_DEFAULTS["cmap"]
    calib_dir: str = Field("", description="Must remain blank; calibration uses the night's directory")
    site: str = ""
    telescope: str = ""
    mode: str = ""
    exclude_after_jd: FormText = Field("", description="Comma-separated Julian dates")
    exclude_before_jd: FormText = Field("", description="Comma-separated Julian dates")
    post_sigma: FormFloat = phot.POSTPROCESS_DEFAULTS["post_sigma"]
    post_poly_deg: FormInt = phot.POSTPROCESS_DEFAULTS["post_poly_deg"]
    post_exclude_before_jd: FormFloat = ""
    post_exclude_after_jd: FormFloat = ""


class DatasetRequest(RequestModel):
    inst: str = ""
    date: str = Field("", description="Observing night, yymmdd")
    target: str = ""


class RunRequest(DatasetRequest):
    test_run: bool = True
    options: PhotometryOptions | None = Field(default_factory=PhotometryOptions)
    confirm_overwrite: str = Field(
        "", description="The confirm_token of a 409 run_exists answer, to overwrite that run (#317)"
    )


class CommandRequest(RunRequest):
    test_run: bool = False


class CancelRequest(DatasetRequest):
    run_id: str = ""
    run: str = Field("", description="Legacy alias; run_id takes precedence")


class PostprocessRequest(DatasetRequest):
    run: str = ""
    sigma: FormFloat = 5.0
    degree: FormInt = 2
    iterations: FormInt = 5
    apply: bool = False
    exclude_before_jd: FormFloat = None
    exclude_after_jd: FormFloat = None


class RestoreRequest(DatasetRequest):
    run: str = ""


class BatchJob(DatasetRequest):
    run: str = ""


class BatchRequest(RequestModel):
    # Keep the endpoint's bounded batch and per-entry error semantics. Validate
    # entries in the handler rather than rejecting the whole batch with 422.
    jobs: SkipValidation[list[BatchJob] | None] = None


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorResponse(ResponseModel):
    error: str


class ActionResponse(ResponseModel):
    ok: bool
    error: str | None = None
    key: str | None = None
    run_id: str | None = None
    queued: bool = False
    already_running: bool = False
    already_finished: bool = False
    limit_exceeded: bool = False
    requested: bool = False
    count: int | None = None


class CommandResponse(ResponseModel):
    command: str
    error: str | None


class JobStatus(ResponseModel):
    state: Literal["none", "pending", "running", "cancelling", "finalizing", "done", "error", "cancelled"]
    log: str
    returncode: int | None
    elapsed: int
    error_desc: str = ""
    run_type: Literal["test", "full"] | None = None


class BatchStatus(JobStatus):
    inst: str
    date: str
    target: str
    run: str


class BatchResponse(ResponseModel):
    jobs: list[BatchStatus | ErrorResponse]


class ClipFile(ResponseModel):
    file: str
    n: int
    n_clipped: int
    n_kept: int
    n_excluded_range: int = 0
    kept_fraction: float
    sigma: float | None = None
    degree: int | None = None
    iterations: int | None = None
    exclude_before_jd: float | None = None
    exclude_after_jd: float | None = None


class PostprocessResponse(ResponseModel):
    ok: bool
    error: str | None = None
    applied: bool = False
    sigma: float | None = None
    degree: int | None = None
    iterations: int | None = None
    exclude_before_jd: float | None = None
    exclude_after_jd: float | None = None
    n_files: int = 0
    files: list[ClipFile] = Field(default_factory=list)
    summary_png: str | None = None
    preview_png: str | None = Field(None, description="Base64 PNG preview")
    restorable: bool = False


class RestoreResponse(ResponseModel):
    ok: bool
    error: str | None = None
    restored: list[str] = Field(default_factory=list)
    n_files: int = 0


class OutputFile(ResponseModel):
    file: str
    created_at: str
    version: str
    key: str | None = None


class PhotometryOutputs(ResponseModel):
    """Filenames are fetched through the /file routes, or together via /download-all."""

    summary: dict[str, OutputFile]
    summary_items: list[OutputFile]
    bands: dict[str, dict[str, OutputFile]]
    npz: str | None
    log: str | None
    has_any: bool
    masters: list[str]
    sites: list[str]
    site: str | None
    telescopes: list[str]
    telescope: str | None
    modes: list[str]
    mode: str | None
    ref_header: str | None
    ref_selection: str | None


class PhotometryRun(ResponseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    run_id: str
    site: str
    telescope: str
    mode: str
    run_name: str
    mtime: float
    is_legacy: bool
    run_type: str


class RunsResponse(ResponseModel):
    runs: list[PhotometryRun]


class RefTimeBand(ResponseModel):
    ccd: int
    band: str
    n: int
    jd_first: float
    jd_last: float
    exptime: float | None


class RefTimeRange(ResponseModel):
    jd_min: float
    jd_max: float
    step_s: int
    n_steps: int
    start_iso: str
    end_iso: str
    bands: list[RefTimeBand]


class RefTimeFrame(ResponseModel):
    ccd: int
    band: str
    frame: str
    jd: float
    offset_s: float
    exptime: float | None
    image: str | None
    source: Literal["raw", "calibrated"] | None
    note: str | None = None
    error: str | None = None


class RefTimePreview(ResponseModel):
    ref_time: str
    jd: float
    calibrated_expected: bool
    bands: list[RefTimeFrame]


def response(model: type[BaseModel], content: dict, status_code: int = 200) -> JSONResponse:
    """Validate direct JSONResponse bodies too, without adding absent keys."""
    body = model.model_validate(content).model_dump(mode="json", exclude_unset=True)
    return JSONResponse(body, status_code=status_code)
