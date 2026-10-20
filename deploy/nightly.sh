#!/usr/bin/env bash
# Nightly ingest + rebuild with a Slack alert on failure (issue #237).
#
#   deploy/nightly.sh run     catalogs -> scan-yesterday -> build-db
#   deploy/nightly.sh check   alert if the database was not rebuilt recently
#
# `run` keeps the old `a && b && c` semantics: the first failing step stops the
# chain, but the failure is now reported with the step name and the tail of
# its log instead of leaving only a traceback in logs/ and a stale
# `last_build_at`. A refused build (the integrity gate, #182) exits non-zero
# and is reported the same way.
#
# `check` is the second, independent signal: a cron entry that never fires
# produces no failure to report. Run it some hours after `run`.
#
# Environment: UV_BIN (default `uv`), MAX_BUILD_AGE_HOURS (default 36),
# SLACK_WEBHOOK_FILE (see deploy/notify-slack.sh).
set -u

MODE="${1:-run}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UV="${UV_BIN:-uv}"
MAX_AGE="${MAX_BUILD_AGE_HOURS:-36}"
cd "$ROOT" || exit 1
mkdir -p logs

alert() {
  bash "$SCRIPT_DIR/notify-slack.sh" "$1"
}

# step <name> <log> <command...>: run one step appending to its log; on failure
# alert with the step name, exit code and the last lines of the log, and stop.
step() {
  local name="$1" log="$2"
  shift 2
  "$@" >> "$log" 2>&1
  local code=$?
  if [ "$code" -ne 0 ]; then
    alert ":rotating_light: muscat-db nightly: ${name} failed (exit ${code}) on $(hostname). Last lines of ${log}:
$(tail -n 15 "$log")"
    exit "$code"
  fi
}

case "$MODE" in
  run)
    step download-catalogs logs/download_catalogs.log bash scripts/download_catalogs.sh
    step scan-yesterday logs/scan.log "$UV" run muscat-db scan-yesterday
    # A leaked-pages snapshot is accepted so the build runs, but the pages may
    # hide lost rows: say so out loud instead of leaving it in the log.
    before=$(wc -c < logs/build-db.log 2>/dev/null || echo 0)
    step build-db logs/build-db.log "$UV" run muscat-db build-db
    leak="$(tail -c +"$((before + 1))" logs/build-db.log 2>/dev/null | grep -m1 "INTEGRITY-LEAK")"
    if [ -n "$leak" ]; then
      alert ":warning: muscat-db nightly on $(hostname): the database has unreferenced pages (integrity_check), so rows may be lost. The build went ahead; the last clean snapshot is kept. See notes/DEPLOYMENT.md, Database integrity failure. ${leak}"
    fi
    ;;
  check)
    out="$("$UV" run muscat-db build-status --max-age-hours "$MAX_AGE" 2>&1)"
    code=$?
    if [ "$code" -ne 0 ]; then
      alert ":rotating_light: muscat-db database is stale on $(hostname): ${out}"
      exit "$code"
    fi
    ;;
  *)
    echo "usage: nightly.sh run|check" >&2
    exit 2
    ;;
esac
