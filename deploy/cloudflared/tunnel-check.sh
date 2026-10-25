#!/usr/bin/env bash
# Health check for the Cloudflare Access tunnel (issue #50), run from cron:
#
#   */5 * * * * bash $MUSCATDB_ROOT/deploy/cloudflared/tunnel-check.sh >> $MUSCATDB_ROOT/logs/tunnel-check.log 2>&1
#
# The tunnel is the only way external collaborators reach the app, and nothing
# on ut2 fails when it drops, so this is the only signal. Two checks:
#
#   1. cloudflared's local readiness endpoint answers 200 (it answers 503 while
#      the tunnel has no connection to Cloudflare's edge).
#   2. With TUNNEL_PUBLIC_URL set, the public hostname redirects to the team's
#      *.cloudflareaccess.com login. Anything else means the hostname is served
#      without the Access login in front. nginx still refuses a request with no
#      valid JWT, but Access is then no longer doing its part.
#
# Alerts Slack (deploy/notify-slack.sh) only when the state changes -- a new
# problem, a different problem, or recovery -- never on every run. The state
# advances only once the alert was delivered, so a lost alert is sent again on
# the next run. Exits 1 while a problem persists so the cron log shows it, and
# 2 when an alert could not be delivered.
#
# Plain bash 3.2 (no ${var,,}), so it behaves the same on a macOS checkout.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
METRICS_URL="${TUNNEL_METRICS_URL:-http://127.0.0.1:20241/ready}"
PUBLIC_URL="${TUNNEL_PUBLIC_URL:-}"
STATE_FILE="${TUNNEL_CHECK_STATE:-$ROOT/logs/tunnel-check.state}"
mkdir -p "$(dirname "$STATE_FILE")"

alert() {
  NOTIFY_SLACK_STRICT=1 bash "$ROOT/deploy/notify-slack.sh" "$1"
}

# Stable keys decide whether the state changed; the details only go in the
# message, so a code flapping between 000 and 503 does not re-alert.
keys=()
details=()

code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$METRICS_URL")"
if [ "$code" != "200" ]; then
  keys+=("tunnel-not-ready")
  details+=("tunnel not ready: ${METRICS_URL} answered ${code} (000: cloudflared not listening)")
fi

if [ -n "$PUBLIC_URL" ]; then
  read -r pcode location < <(curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' -m 10 "$PUBLIC_URL")
  host="${location#*://}"
  host="${host%%/*}"
  host="${host%%:*}"
  host="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')"
  case "$pcode" in
    301|302|303|307) ;;
    *) host="" ;;
  esac
  case "$host" in
    *.cloudflareaccess.com) ;;
    *)
      keys+=("public-not-behind-access")
      details+=("public hostname not behind Cloudflare Access: ${PUBLIC_URL} answered ${pcode}${location:+ -> ${location}}")
      ;;
  esac
fi

current="ok"
if [ "${#keys[@]}" -gt 0 ]; then
  current="$(IFS=,; echo "${keys[*]}")"
fi
previous="$(cat "$STATE_FILE" 2>/dev/null || echo ok)"
message="$(IFS=';'; echo "${details[*]:-}")"

echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') ${current}${message:+: ${message}}"

if [ "$current" != "$previous" ]; then
  if [ "$current" = "ok" ]; then
    text=":white_check_mark: muscat-db tunnel on $(hostname) recovered (was: ${previous})"
  else
    text=":rotating_light: muscat-db tunnel on $(hostname): ${message}. External collaborators may be locked out. See notes/DEPLOYMENT.md, Cloudflare Access tunnel."
  fi
  if ! alert "$text"; then
    echo "alert not delivered; the state stays '${previous}' so the next run sends it again"
    exit 2
  fi
  printf '%s\n' "$current" > "$STATE_FILE"
fi

[ "$current" = "ok" ]
