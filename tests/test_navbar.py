"""Contracts for the shared current-page navbar treatment."""

import re
from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
BASE_HTML = ROOT / "src" / "muscat_db" / "templates" / "base.html"
STYLES_CSS = ROOT / "src" / "muscat_db" / "static" / "styles.css"

# Sections resolved by a prefix/regex rule in the script rather than a direct
# ``section`` map entry for their own nav link's href (e.g. "lco" matches via
# ``path.indexOf('/lco/') === 0``, which already covers "/lco/schedule").
_PREFIX_MATCHED_SECTIONS = {"lco"}


def _parse_section_map(base_html: str) -> dict[str, str]:
    body = re.search(r"var section = \{(.*?)\};", base_html, re.S).group(1)
    return dict(re.findall(r"'([^']+)':\s*'([^']+)'", body))


def test_visible_nav_links_have_unique_sections():
    soup = BeautifulSoup(BASE_HTML.read_text(), "html.parser")
    visible_links = [
        link for link in soup.select("nav a")
        if "display: none" not in link.get("style", "")
    ]
    sections = [link.get("data-nav-section") for link in visible_links]

    assert all(sections)
    assert len(sections) == len(set(sections))


def test_current_page_is_accessible_and_subtly_styled():
    base = BASE_HTML.read_text()
    styles = STYLES_CSS.read_text()

    assert "setAttribute('aria-current', 'page')" in base
    assert "path.indexOf('/lco/') === 0" in base
    assert "muscat2|muscat3|muscat4|sinistro" in base
    assert 'nav a[aria-current="page"]' in styles
    assert "text-decoration-thickness: 2px" in styles


def test_every_static_nav_link_resolves_to_its_own_section():
    """Regression (#62): the Targets link's href ("/targets") had no matching
    entry in the JS ``section`` lookup, so visiting /targets never set
    ``aria-current`` on it and it never got the active-page underline every
    other nav link gets. Every nav link whose href isn't handled by a
    prefix/regex special case must be a key in the map, resolving to its own
    ``data-nav-section`` value."""
    soup = BeautifulSoup(BASE_HTML.read_text(), "html.parser")
    section_map = _parse_section_map(BASE_HTML.read_text())

    links = [
        link for link in soup.select("nav a[data-nav-section]")
        if link["data-nav-section"] not in _PREFIX_MATCHED_SECTIONS
    ]
    assert links
    for link in links:
        href = link["href"]
        section = link["data-nav-section"]
        assert href in section_map, f"{href!r} ({section!r}) missing from JS section map"
        assert section_map[href] == section


def test_individual_target_page_has_no_nav_item_of_its_own():
    """An individual target page (/target?name=...) lives under the Targets
    nav item rather than getting a separate "Target" one: there is no
    ``data-nav-section="target"`` link, and /target's own section map entry
    points at "targets" so viewing one still highlights Targets."""
    soup = BeautifulSoup(BASE_HTML.read_text(), "html.parser")
    section_map = _parse_section_map(BASE_HTML.read_text())

    assert soup.select_one('nav a[data-nav-section="target"]') is None
    assert soup.select_one("#target-nav-link") is None
    assert section_map["/target"] == "targets"
