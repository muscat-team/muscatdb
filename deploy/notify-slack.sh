#!/usr/bin/env bash
# Post one message to the Slack webhook (issue #237).
#
#   deploy/notify-slack.sh "text"
#
# Same webhook file and convention as deploy/pull-deploy.sh: a root-owned
# directory, a file readable only by the account that runs the cron job, never
# committed and never printed. This script must not itself become a silent
# failure, so it always echoes the message to stderr (cron mails/logs it) and
# by default always exits 0 -- a Slack outage must not mask the original
# failure code.
#
# NOTIFY_SLACK_STRICT=1 instead exits 1 when the message was not delivered (no
# readable webhook, or the post failed), for a caller that alerts only on a
# change of state and must retry a lost alert (deploy/cloudflared/tunnel-check.sh).
set -u

SLACK_WEBHOOK_FILE="${SLACK_WEBHOOK_FILE:-/etc/muscat-db/slack-webhook-url}"
text="${1:?usage: notify-slack.sh \"text\"}"

echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') $text" >&2

delivered=0
if [ -r "$SLACK_WEBHOOK_FILE" ]; then
  webhook="$(cat "$SLACK_WEBHOOK_FILE")"
  if [ -n "$webhook" ]; then
    payload="$(python3 -I -c 'import json, sys; print(json.dumps({"text": sys.argv[1]}))' "$text")"
    if curl -fsS -m 10 -X POST -H 'Content-type: application/json' \
      --data "$payload" "$webhook" >/dev/null 2>&1; then
      delivered=1
    fi
  fi
fi
if [ "${NOTIFY_SLACK_STRICT:-0}" = "1" ] && [ "$delivered" != "1" ]; then
  echo "notify-slack: message not delivered to Slack" >&2
  exit 1
fi
exit 0
