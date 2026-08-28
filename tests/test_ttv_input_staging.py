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
