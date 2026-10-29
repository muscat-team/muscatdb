"""User-guide grounding for the ``@bot`` assistant.

@bot answers with the access of a regular (non-admin) user, so it is grounded
on the user guide (``templates/guide.html``, served at ``/guide``) rather than
CLAUDE.md, which is the developers' and agents' handbook (hosts, tmux, backups,
the release process).

The guide is split into chunks: one per workflow card and one per FAQ answer.
Admin material is dropped twice over: by title (the "Multi-User Auth &
Deployment" card and the "Deployment & Multi-User" FAQ category) and by content
(any chunk naming admin tooling such as htpasswd or the proxy secret), so a
section added later that drifts into server administration is caught too.

For each question the best-matching chunks are picked by keyword overlap and
capped at ``MAX_GUIDE_CHARS``; a list of the remaining section titles is always
appended so the model can point the user at the right part of /guide.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

GUIDE_PATH = Path(__file__).resolve().parent / "templates" / "guide.html"

MAX_GUIDE_CHARS = 7000  # matched chunks per question (num_ctx is 8192 tokens)
_MAX_CHUNK_CHARS = 3000  # one chunk never crowds out the rest
_MIN_RELATIVE_SCORE = 0.25  # keep chunks scoring at least this share of the best

_ADMIN_TITLE_RE = re.compile(r"deployment", re.IGNORECASE)
_ADMIN_TEXT_RE = re.compile(
    r"htpasswd|\bsudo\b|systemctl|\btmux\b|crontab|\.web-env|proxy[ _-]secret"
    r"|cloudflared|ssh -L|MUSCAT_OLLAMA",
    re.IGNORECASE,
)

# Template regions and markup that never reach the model. <pre> holds the
# mermaid workflow diagrams, which are noise as plain text.
_CONTENT_RE = re.compile(r"\{%\s*block content\s*%\}(.*?)\{%\s*block scripts\s*%\}", re.DOTALL)
_DROP_RE = re.compile(r"<(script|style|pre)\b.*?</\1>|<!--.*?-->|\{[%{#].*?[%}#]\}", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_FAQ_RE = re.compile(
    r"<h3\b[^>]*>(?P<category>.*?)</h3>"
    r"|<div class=\"faq-item\">(?P<item>.*?)(?=<div class=\"faq-item\">|<h3\b|\Z)",
    re.DOTALL,
)
_FAQ_QUESTION_RE = re.compile(r"class=\"faq-question\"[^>]*>(.*?)</div>", re.DOTALL)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.+-]{2,}")
_STOPWORDS = frozenset(
    "the and for are you can how what why when where which who does with this that "
    "from into about have has was were will would should could there their them then "
    "than use using used get set any all not but our your its it's bot muscat-db".split()
)


@dataclass(frozen=True)
class GuideChunk:
    title: str
    text: str


def _plain(fragment: str) -> str:
    text = _TAG_RE.sub(" ", _DROP_RE.sub(" ", fragment))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _faq_chunks(card: str) -> list[GuideChunk]:
    chunks: list[GuideChunk] = []
    category = ""
    for m in _FAQ_RE.finditer(card):
        if m.group("category") is not None:
            category = _plain(m.group("category"))
            continue
        item = m.group("item")
        q = _FAQ_QUESTION_RE.search(item)
        question = _plain(q.group(1)).rstrip(" +") if q else ""
        answer = _plain(item[q.end() :] if q else item)
        chunks.append(GuideChunk(f"FAQ › {category} › {question}", answer))
    return chunks


def _split(content: str) -> list[GuideChunk]:
    chunks: list[GuideChunk] = []
    for card in re.split(r"(?=<h2\b)", content)[1:]:  # [0] is the TOC + overview diagram
        head, _, body = card.partition("</h2>")
        title = _plain(head)
        if "Frequently Asked Questions" in title:
            chunks.extend(_faq_chunks(body))
        else:
            chunks.append(GuideChunk(title, _plain(body)))
    return chunks


def _is_admin(chunk: GuideChunk) -> bool:
    return bool(_ADMIN_TITLE_RE.search(chunk.title) or _ADMIN_TEXT_RE.search(chunk.text))


@lru_cache(maxsize=1)
def guide_chunks() -> tuple[GuideChunk, ...]:
    """The guide's user-facing chunks (cached; guide edits need a restart, like
    the templates themselves). A missing or unreadable guide yields ``()``."""
    try:
        source = GUIDE_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("chat guide: %s not readable; @bot has no guide grounding", GUIDE_PATH)
        return ()
    m = _CONTENT_RE.search(source)
    chunks = _split(m.group(1) if m else source)
    return tuple(
        GuideChunk(c.title, c.text[:_MAX_CHUNK_CHARS])
        for c in chunks
        if c.text and not _is_admin(c)
    )


def _terms(question: str) -> set[str]:
    return {w.strip(".-") for w in _WORD_RE.findall(question.lower())} - _STOPWORDS - {""}


def _score(chunk: GuideChunk, terms: set[str]) -> int:
    title, text = chunk.title.lower(), chunk.text.lower()
    return sum(3 * title.count(t) + min(text.count(t), 5) for t in terms)


def _topics(chunks: tuple[GuideChunk, ...]) -> list[str]:
    """Section names for the table of contents: cards, and FAQ categories."""
    seen: dict[str, None] = {}
    for c in chunks:
        parts = c.title.split(" › ")
        seen.setdefault(" › ".join(parts[:2]) if parts[0] == "FAQ" else c.title, None)
    return list(seen)


def guide_grounding(question: str) -> str:
    """A system-message block with the guide chunks most relevant to
    ``question`` plus the guide's table of contents, or ``""`` without a guide."""
    chunks = guide_chunks()
    if not chunks:
        return ""
    terms = _terms(question or "")
    ranked = sorted(
        ((s, i) for i, c in enumerate(chunks) if (s := _score(c, terms)) > 0),
        key=lambda pair: (-pair[0], pair[1]),
    )
    # Weak matches (a long section that shares a common word) would only pad
    # the context and pull the answer off topic.
    floor = ranked[0][0] * _MIN_RELATIVE_SCORE if ranked else 0
    picked: list[GuideChunk] = []
    used = 0
    for score, i in ranked:
        if score < floor:
            break
        chunk = chunks[i]
        cost = len(chunk.title) + len(chunk.text)
        if used + cost > MAX_GUIDE_CHARS:
            continue
        picked.append(chunk)
        used += cost

    parts = [
        "Reference: the muscat-db user guide (the /guide page). Ground your answer "
        "in it, and point the user to the matching section as a Markdown link to "
        "/guide, e.g. [LCO Archive Download Workflow](/guide)."
    ]
    parts += [f"### {c.title}\n{c.text}" for c in picked]
    parts.append("Guide sections: " + "; ".join(_topics(chunks)))
    return "\n\n".join(parts)
