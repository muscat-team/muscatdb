# Contributing to muscat-db

## Setup

```bash
uv sync --dev
uv run pre-commit install   # runs `ruff check` before each commit
```

Copy `.env.example` to `.env` only if you need to override a default (see
[Configuration](README.md#configuration)).

## Before opening a PR

```bash
uv run ruff check .
uv run pytest -q                 # fast suite (heavyweight `slow`-marked tests are deselected by default)
uv run pytest -m slow            # only on a host with the real prose/timer/harmonic conda envs and data
uv run pytest --cov=src/muscat_db --cov-report=term-missing   # coverage gate in CI is 68%, target 80%
```

CI (`.github/workflows/ci.yml`) runs `ruff`, the fast suite on Python 3.12 and
3.13, the PostgreSQL control-plane suite, the coverage gate, and a strict
MkDocs build. All of it must be green before merge; branch rulesets require
one approving review and up-to-date status checks (see below).

## Branch workflow

- Branch off `test`, not `main`. Open your PR against `test` — it's the
  default branch, so a new PR targets it without being told to.
- Never PR a feature branch straight into `main`, and never branch off
  another open PR's head (if that PR merges into `test` first, GitHub closes
  yours along with it, since its base branch just disappeared). Wait for the
  first PR to land on `test`, then branch from `test` again.
- Don't push to someone else's PR branch, and don't open a PR against it
  either — that's stacking. If you want a change made, leave it in review and
  let the author implement it.
- Never rename an open PR's head branch; GitHub closes the PR instead of
  retargeting it.
- `test` accumulates features and is periodically merged into `main` as a
  release (maintainers only; that PR always merges with a merge commit, never
  squash, to keep `test` and `main` history compatible).
- Both branches are protected by rulesets: no direct pushes, no force-push or
  deletion, one approving review, and passing status checks required. Org
  admins can bypass this, but it's a convention even for them.
- Re-request review only once you're finished pushing. A push voids any
  approval already given, so a review of a branch that's still receiving
  commits just has to be redone. If you do push again after re-requesting,
  leave a short comment saying so — there's no other way for the reviewer to
  tell.

## Commits and issues

- Conventional-commit-style subjects: `feat:`, `fix:`, `refactor:`, `docs:`,
  `test:`, `chore:`, `perf:`, `ci:`.
- A bare `#N` in a commit or PR body resolves against this repo. Reference an
  issue in another muscat-team repo (`prose2`, `timer`, `harmonic`) as
  `owner/repo#N`.
- `Closes #N` in a feature PR closes the issue when that PR merges into
  `test` — before the fix actually reaches production, since `deploy.yml`
  only runs on a push to `main`. If the issue should stay open until release,
  write `Refs #N` instead and close it by hand at release time.

## Full policy

The items above are the parts a human contributor needs. `AGENTS.md` (the
target of `CLAUDE.md`) has the fuller, more mechanical version of this same
policy — exact ruleset IDs, review-dismissal behavior, and the specific
incidents that motivated each rule — kept there because it's written for, and
kept current by, the coding agents operating on this repo.
