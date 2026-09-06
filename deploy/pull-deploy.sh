#!/usr/bin/env bash
# Pull-based deploy for one checkout, run from cron (see issue #131).
#
# deploy.yml's push-triggered SSH deploy could never reach this host: it runs
# on GitHub-hosted runners, and this host is not reachable by SSH from
# outside without the VPN. Instead, this script polls this checkout's remote
# branch and, on a new SHA, resets to it and relaunches the app -- outbound
# only, no inbound path, no runner, no deploy key held by GitHub.
#
# Usage:  deploy/pull-deploy.sh <branch> <tmux-session> <port>
#
# Intended to run as the deployed copy inside each checkout (e.g.
# $HOME/deploy/main/app/deploy/pull-deploy.sh), from cron, so the checkout it
# deploys and the script that deploys it are the same tracked, reviewable
# file -- not a separate copy living only on the host. See
# notes/deploy-staging-plan.md Gate F for the cron entries.

set -euo pipefail

usage() {
  echo "usage: $0 <branch> <tmux-session> <port>" >&2
  exit 2
}
[ $# -ge 3 ] || usage

BRANCH="$1"
TMUX_SESSION="$2"
PORT="$3"

UV="${UV_BIN:-/ut2/jerome/.local/bin/uv}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_PATH="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$DEPLOY_PATH"

# Same convention as /etc/muscat-db/proxy-secret (deploy/setup-nginx.sh):
# root-owned directory, secret file readable only by the deploy account. Not
# committed, not printed -- create it by hand before relying on failure
# alerts: `install -o "$(whoami)" -g root -m 600 /dev/stdin
# /etc/muscat-db/slack-webhook-url <<< 'https://hooks.slack.com/...'`
SLACK_WEBHOOK_FILE="${SLACK_WEBHOOK_FILE:-/etc/muscat-db/slack-webhook-url}"

notify_slack_failure() {
  # Accepts an explicit exit code (used when we fail a check ourselves, e.g.
  # the health check below) so callers aren't relying on `set -e`/ERR-trap
  # semantics, which do NOT fire for a bare `exit N` -- only for a command
  # that actually returns non-zero. Falls back to `$?` for the ERR-trap case.
  local exit_code="${1:-$?}"
  if [ -r "$SLACK_WEBHOOK_FILE" ]; then
    local webhook
    webhook="$(cat "$SLACK_WEBHOOK_FILE")"
    if [ -n "$webhook" ]; then
      local text=":rotating_light: pull-deploy failed -- branch ${BRANCH}, $(hostname), exit ${exit_code}. Check ${DEPLOY_PATH}/logs/pull-deploy.log."
      curl -fsS -m 10 -X POST -H 'Content-type: application/json' \
        --data "{\"text\":\"${text}\"}" "$webhook" >/dev/null 2>&1 || true
    fi
  fi
  # A dead cron job is the failure mode this whole script exists to avoid --
  # a missing/unreadable webhook file must not itself go silent, so it still
  # prints here even though it can't reach Slack.
  echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') FAILED (exit ${exit_code}), branch ${BRANCH}" >&2
  exit "$exit_code"
}
trap notify_slack_failure ERR

# The gate below compares the remote against the last *verified* deploy, not
# against HEAD: `git reset --hard` makes HEAD match the remote before uv sync
# or the relaunch even run, so gating on HEAD means a failure partway through
# (uv sync, or the health check below) is recorded as done -- the next poll
# sees HEAD == remote and no-ops forever, silently, with no further alert.
# This file only ever moves forward past a successful health check, so a
# stuck deploy keeps being retried (and keeps alerting) on every poll until it
# actually succeeds or a human intervenes.
STATE_DIR="$DEPLOY_PATH/logs"
mkdir -p "$STATE_DIR"
LAST_GOOD_SHA_FILE="${PULL_DEPLOY_MARKER_FILE:-$STATE_DIR/pull-deploy-last-good-sha}"

# `tmux send-keys`/`new-session` only confirm the pane accepted keystrokes,
# not that the process inside it is running -- verified against a real tmux
# session with a nonexistent command: exit 0, pane shows "command not found".
# Poll the app's own liveness probe instead of trusting tmux's exit status.
HEALTH_CHECK_TIMEOUT_S="${PULL_DEPLOY_HEALTH_TIMEOUT_S:-30}"
HEALTH_CHECK_INTERVAL_S="${PULL_DEPLOY_HEALTH_INTERVAL_S:-2}"

wait_for_healthy() {
  local deadline=$(($(date +%s) + HEALTH_CHECK_TIMEOUT_S))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$HEALTH_CHECK_INTERVAL_S"
  done
  return 1
}

if [ "${PULL_DEPLOY_REEXECED:-0}" != "1" ]; then
  # First run ever against this checkout: there is no prior verified deploy
  # to compare against yet. Whatever is running now was brought up by hand
  # (Gate F's bootstrap, already verified healthy) -- trust it and start
  # tracking from here, rather than forcing an immediate redeploy of the
  # exact commit that's already live.
  if [ ! -f "$LAST_GOOD_SHA_FILE" ]; then
    git rev-parse HEAD > "$LAST_GOOD_SHA_FILE"
  fi
  LAST_GOOD_SHA="$(cat "$LAST_GOOD_SHA_FILE")"

  REMOTE_SHA="$(git ls-remote origin "refs/heads/$BRANCH" | cut -f1)"
  [ -n "$REMOTE_SHA" ] || { echo "could not resolve origin/$BRANCH via ls-remote" >&2; notify_slack_failure 1; }

  if [ "$LAST_GOOD_SHA" = "$REMOTE_SHA" ]; then
    exit 0
  fi

  echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') deploying $BRANCH: $LAST_GOOD_SHA -> $REMOTE_SHA"
  git fetch origin "$BRANCH"
  git reset --hard "origin/$BRANCH"

  # git reset --hard just rewrote this script's own bytes on disk -- it is
  # deploy/pull-deploy.sh inside the checkout it deploys -- and bash reads a
  # running script's file incrementally as execution proceeds rather than
  # loading it whole up front. Continuing in this same process risks reading
  # torn or stale bytes for everything past this point. Re-exec a fresh
  # process instead, so the rest of the deploy runs from what is actually on
  # disk now.
  PULL_DEPLOY_REEXECED=1 exec "$0" "$@"
fi

"$UV" sync --dev

tmux send-keys -t "$TMUX_SESSION" "" C-c || true
sleep 2
# send-keys targets an existing pane's own shell, which never saw this
# script's cd above, so the launch command carries its own cd (same reason
# the deploy.yml block it replaces did the same thing).
tmux send-keys -t "$TMUX_SESSION" "cd '$DEPLOY_PATH' && $UV run uvicorn muscat_db.web:sio_app --host 127.0.0.1 --port $PORT" Enter || \
  tmux new-session -d -s "$TMUX_SESSION" -c "$DEPLOY_PATH" "$UV run uvicorn muscat_db.web:sio_app --host 127.0.0.1 --port $PORT"

if ! wait_for_healthy; then
  echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') health check failed for $BRANCH on 127.0.0.1:$PORT/healthz after relaunch" >&2
  notify_slack_failure 1
fi

# Only advance past a verified-healthy deploy -- see the note on
# LAST_GOOD_SHA_FILE above for why gating on this instead of HEAD matters.
git rev-parse HEAD > "$LAST_GOOD_SHA_FILE"
echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') deployed $BRANCH at $(git rev-parse HEAD)"
