"""Small interaction helpers shared by the browser tests."""

from __future__ import annotations


def reveal(page, selector: str) -> None:
    """Open every collapsed <details> around *selector*, outermost first.

    Clicks each section's own summary, as a user would, so toggle handlers
    (lazy loads, saved open/closed state) run as they do in real use.
    """
    depth = page.eval_on_selector(
        selector,
        """el => { let n = 0; for (let d = el.parentElement; d; d = d.parentElement)
                     if (d.tagName === 'DETAILS' && !d.open) n++; return n; }""",
    )
    for _ in range(depth):
        page.locator(selector).locator(
            "xpath=ancestor::details[not(@open)][last()]/summary"
        ).click()
