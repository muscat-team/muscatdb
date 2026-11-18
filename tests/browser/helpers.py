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


class QueuedJobStore:
    """A job store whose only row is one queued job, keyed like the real store.

    Answers ``get`` for *key* only, so a page that builds the wrong key does not
    find the job. Listing calls return nothing, as with an otherwise empty queue.
    """

    def __init__(self, key: str, state: str = "pending") -> None:
        self.key = key
        self.state = state

    def get(self, key: str) -> dict | None:
        return {"key": key, "state": self.state} if key == self.key else None

    def all(self) -> list[dict]:
        return []

    def active(self) -> list[dict]:
        return []
