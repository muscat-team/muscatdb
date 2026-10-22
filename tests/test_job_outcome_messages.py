"""A failed job's description says what happened, not just the last log line.

``error_desc`` (Jobs page, photometry status) was the last log line that
mentioned an error, cut at 100 characters, or one of "Unknown error",
"Empty log file", "Failed to parse log". A job the kernel killed for memory
left a log that stopped mid-stream, so its description was an unrelated older
warning; and a description cut mid-word gave no hint the full text was in the
log.
"""

from __future__ import annotations

import pytest

from muscat_db import jobs
from muscat_db.photometry import _get_error_desc

# -- exit codes ----------------------------------------------------------------


@pytest.mark.parametrize("rc,expected", [
    (-9, "The process was killed (SIGKILL), most often because the server ran out of memory"),
    (137, "The process was killed (SIGKILL), most often because the server ran out of memory"),
    (-7, "The process crashed with a bus error (SIGBUS)"),
    (-11, "The process crashed with a segmentation fault (SIGSEGV)"),
    (-15, "The process was terminated (SIGTERM)"),
    (-2, "The process was stopped by signal 2"),
    (2, "The process exited with code 2"),
])
def test_exit_codes_read_in_plain_words(rc, expected):
    assert jobs.describe_exit(rc) == expected


@pytest.mark.parametrize("rc", [None, 0, jobs.CANCELLED_RC])
def test_no_description_for_success_unknown_or_our_cancel_marker(rc):
    assert jobs.describe_exit(rc) == ""


# -- the description of a failed job -------------------------------------------


def test_missing_log_says_the_cause_is_unknown_and_gives_the_exit(tmp_path):
    msg = _get_error_desc(tmp_path / "absent.log", returncode=2)
    assert msg.startswith("The process exited with code 2 and left no log")
    assert "contact an admin" in msg


def test_empty_log_says_the_engine_probably_did_not_start(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("\n\n")
    msg = _get_error_desc(log, returncode=1)
    assert "before writing any output" in msg


def test_a_signal_death_is_named_before_the_last_log_error(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("INFO start\nWARNING - ERROR: an older recoverable error\nINFO aligning frame 812\n")
    msg = _get_error_desc(log, returncode=-9)
    assert msg.startswith("The process was killed (SIGKILL)")
    assert "an older recoverable error" in msg


def test_a_plain_failure_keeps_the_log_error_alone(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("x - ERROR: no stars detected in the reference frame\n")
    assert _get_error_desc(log, returncode=1) == "no stars detected in the reference frame"


def test_a_long_error_is_cut_visibly_and_points_to_the_log(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("x - ERROR: " + "a" * 400 + "\n")
    msg = _get_error_desc(log)
    assert "a" * 200 in msg
    assert msg.endswith("… (see the log for the full message)")


def test_a_log_with_no_error_line_says_so(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("INFO step 1\nINFO step 2\n")
    msg = _get_error_desc(log, returncode=3)
    assert msg.startswith("The process exited with code 3.")
    assert "no error line in the log; its last line was: INFO step 2" in msg


def test_the_old_call_without_a_return_code_still_works(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("x - ERROR: boom\n")
    assert _get_error_desc(log) == "boom"


def test_watchdog_kill_explains_itself():
    msg = jobs.watchdog_desc("no log output for 25 min")
    assert msg.startswith("watchdog: no log output for 25 min.")
    assert "stopped" in msg and "re-run" in msg
