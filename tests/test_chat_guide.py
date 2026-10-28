"""@bot grounding from the user guide (guide.html), scoped to a regular user."""

from __future__ import annotations

import re

import pytest

from muscat_db import chat_guide


@pytest.fixture(autouse=True)
def _fresh_cache():
    chat_guide.guide_chunks.cache_clear()
    yield
    chat_guide.guide_chunks.cache_clear()


def test_guide_splits_into_sections_and_faq_items():
    chunks = chat_guide.guide_chunks()
    titles = [c.title for c in chunks]
    assert len(chunks) > 30
    assert "LCO Archive Download Workflow" in titles
    assert any(t.startswith("FAQ › Database › How does the database work") for t in titles)
    # Markup, diagrams and template tags never reach the model.
    for c in chunks:
        # Real tags only: escaped text such as "scan <inst> <yymmdd>" or
        # "P rot < 10 d" is legitimate guide content.
        assert not re.search(
            r"</?(div|span|p|a|code|li|ul|ol|h\d|strong|em|br|table|pre|script|style)\b[^>]*>",
            c.text,
        ), c.title
        assert "{%" not in c.text and "-->" not in c.text


def test_guide_drops_admin_material():
    chunks = chat_guide.guide_chunks()
    assert not any("deployment" in c.title.lower() for c in chunks)
    joined = " ".join(c.text for c in chunks).lower()
    for marker in ("htpasswd", "proxy secret", "muscat_ollama", "systemctl", "tmux"):
        assert marker not in joined


def test_grounding_picks_the_relevant_sections():
    grounding = chat_guide.guide_grounding("how do I download frames from the LCO archive?")
    assert "LCO Archive Download Workflow" in grounding
    assert "Exposure Calculator" not in grounding.split("Guide sections:")[0]


def test_grounding_is_bounded_and_lists_topics():
    grounding = chat_guide.guide_grounding(
        "photometry transit fit database catalog exposure archive"
    )
    assert len(grounding) <= chat_guide.MAX_GUIDE_CHARS + 2000  # + header and topic list
    assert "Guide sections:" in grounding  # the table of contents is always offered


def test_grounding_without_a_match_still_lists_topics():
    grounding = chat_guide.guide_grounding("hello")
    assert "Guide sections:" in grounding
    assert "Multi-User Auth" not in grounding


def test_missing_guide_yields_no_grounding(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_guide, "GUIDE_PATH", tmp_path / "absent.html")
    assert chat_guide.guide_chunks() == ()
    assert chat_guide.guide_grounding("anything") == ""
