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
  local exit_code=$?
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

if [ "${PULL_DEPLOY_REEXECED:-0}" != "1" ]; then
  LOCAL_SHA="$(git rev-parse HEAD)"
  REMOTE_SHA="$(git ls-remote origin "refs/heads/$BRANCH" | cut -f1)"
  [ -n "$REMOTE_SHA" ] || { echo "could not resolve origin/$BRANCH via ls-remote" >&2; exit 1; }

  if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
    exit 0
  fi

  echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') deploying $BRANCH: $LOCAL_SHA -> $REMOTE_SHA"
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

echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') deployed $BRANCH at $(git rev-parse HEAD)"
