"""The ephemeris page escapes free-text run and target names before rendering.

A transit-fit run name is whatever its author typed, and the ephemeris page
shows every fit of a target to every viewer of that target. The dataset table,
the source dropdown and the O-C hover text build HTML strings from those
values, so an unescaped name runs as script in another user's browser.
"""

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

EPHEMERIS_HTML = Path(__file__).parent.parent / "src" / "muscat_db" / "templates" / "ephemeris.html"

# ${d.run_name}, ${ p.target }, ... with no escaping call around the value.
_BARE_FIELD_RE = re.compile(r"\$\{\s*[\w.]+\.(run_name|target)\s*\}")

node = shutil.which("node")


def _template() -> str:
    return EPHEMERIS_HTML.read_text(encoding="utf-8")


def test_run_name_is_never_interpolated_unescaped():
    bare = [
        (n, line.strip())
        for n, line in enumerate(_template().splitlines(), 1)
        if any(m.group(1) == "run_name" for m in _BARE_FIELD_RE.finditer(line))
    ]
    assert bare == [], f"unescaped run_name interpolation: {bare}"


def test_target_is_never_interpolated_unescaped_into_html():
    # A bare target is fine in a plain key or text string; inside markup it is not.
    bare = [
        (n, line.strip())
        for n, line in enumerate(_template().splitlines(), 1)
        if "<" in line
        and any(m.group(1) == "target" for m in _BARE_FIELD_RE.finditer(line))
    ]
    assert bare == [], f"unescaped target interpolation in markup: {bare}"


def _escape_attr(value: str) -> str:
    match = re.search(r"function escapeAttr\(v\) \{.*?\n  \}", _template(), re.S)
    assert match, "escapeAttr not found in ephemeris.html"
    script = textwrap.dedent(
        f"""
        {match.group(0)}
        console.log(JSON.stringify(escapeAttr({json.dumps(value)})));
        """
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(node is None, reason="node not installed")
def test_escape_attr_neutralises_markup_and_both_quote_styles():
    escaped = _escape_attr("""<img src=x onerror="alert(1)">');alert('x""")
    assert not set("<>\"'") & set(escaped)
    assert escaped == (
        "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&#39;);alert(&#39;x"
    )
