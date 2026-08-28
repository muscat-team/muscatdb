"""harmonic's ``-i``/``-o`` must never point at the same directory (#77).

harmonic upstream ``master`` unconditionally copies its ``-i`` input onto
``<outdir>/data.csv`` at the start of every run. Before this fix, muscat-db
wrote ``data.csv``/``config.ini`` straight into the run directory and passed
that same directory for both ``-i`` and ``-o`` — a copy onto itself, which
raises ``SameFileError`` before any fitting starts. The currently-checked-out
harmonic fork papers over this with an engine-side change; the fix here is on
the muscat-db side per AGENTS.md, so it holds regardless of which harmonic
checkout is in use.
"""

from __future__ import annotations

import json
import pathlib
import shlex

from muscat_db import ttv_fit


def test_write_ttv_inputs_stages_outside_rdir(tmp_path):
    rdir = tmp_path / "TOI-123" / "_runs" / "default"
    rdir.mkdir(parents=True)

    input_dir = ttv_fit.write_ttv_inputs(
        rdir, "planet,epoch,tc,tc_unc\n", "[INIT]\n", {},
    )

    assert input_dir == rdir / "_input"
    assert (input_dir / "data.csv").is_file()
    assert (input_dir / "config.ini").is_file()
    # The old, colliding location must not be where inputs land.
    assert not (rdir / "data.csv").is_file()
    assert not (rdir / "config.ini").is_file()
    # meta.yaml is still written directly under rdir.
    assert (rdir / "meta.yaml").is_file()


def test_get_ttv_command_never_collides_input_and_output_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    monkeypatch.setattr(ttv_fit, "_harmonic_prefix", lambda: ["harmonic"])

    cmd_str = ttv_fit.get_ttv_command(
        "TOI-123", {"run_name": "r1", "planet_letters": "b"},
    )
    cmd = shlex.split(cmd_str)

    i_path = cmd[cmd.index("-i") + 1]
    o_path = cmd[cmd.index("-o") + 1]
    assert i_path != o_path
    # -i's parent must not be -o itself, or harmonic's own -o copy step
    # (copying -i onto <outdir>/data.csv) would be a copy onto itself.
    assert pathlib.Path(i_path).parent != pathlib.Path(o_path)


def test_sync_jobs_queue_drain_stages_inputs_like_start_ttv_fit(tmp_path, monkeypatch):
    """A job that misses its concurrency slot in ``start_ttv_fit`` is queued and
    later launched from ``sync_jobs``'s drain loop instead. That launch path
    must build ``-i``/``-c`` from the same staged ``_input`` directory
    ``write_ttv_inputs`` returns -- not from ``rdir`` directly, which is where
    ``write_ttv_inputs`` no longer writes (#77)."""
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    monkeypatch.setattr(ttv_fit, "_harmonic_prefix", lambda: ["harmonic"])
    # _write_log_banner calls this, which otherwise shells out via
    # subprocess.run -- itself implemented on top of Popen, so it would also
    # hit the fake Popen below and pollute captured_cmds.
    monkeypatch.setattr(ttv_fit, "_harmonic_version", lambda: "0.0.0")

    fake_jobs: dict = {}
    monkeypatch.setattr(ttv_fit, "_TTV_JOBS", fake_jobs)

    captured_cmds = []

    class _Proc:
        pid = 1234

        def poll(self):
            return None

    def _fake_popen(cmd, **_kwargs):
        captured_cmds.append(cmd)
        return _Proc()

    monkeypatch.setattr(ttv_fit.subprocess, "Popen", _fake_popen)

    entry = {
        "target": "TOI-123",
        "run_name": "r1",
        "started_at": 0.0,
        "params": json.dumps({"options": {
            "csv_content": "planet,epoch,tc,tc_unc\n",
            "ini_content": "[INIT]\n",
            "run_name": "r1",
            "planet_letters": "b",
        }}),
    }

    class _FakeStore:
        def all(self):
            return []

        def reconcile_slots(self, pipeline):
            return 0

        def count_claimed(self, pipeline):
            return 0

        def pending(self, pipeline):
            return [entry]

        def claim_slot(self, pipeline, holder_key, max_slots):
            return True

        def save(self, **kwargs):
            pass

    monkeypatch.setattr(ttv_fit, "get_job_store", lambda: _FakeStore())

    try:
        ttv_fit.sync_jobs()

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        i_path = pathlib.Path(cmd[cmd.index("-i") + 1])
        o_path = pathlib.Path(cmd[cmd.index("-o") + 1])
        assert i_path.is_file(), (
            "the file passed as -i must actually exist -- write_ttv_inputs "
            "stages under _input/, not rdir directly"
        )
        assert i_path.parent != o_path
    finally:
        for job in fake_jobs.values():
            job.logf.close()
