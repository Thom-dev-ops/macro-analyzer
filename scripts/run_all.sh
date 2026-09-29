#!/bin/bash
# Macro Analyzer — "run all" launcher.
#
# Starts the three long-lived local processes if they are not already running:
#   1. Telegram listener  — live KOL chart capture + auto-extract
#   2. Ingest scheduler   — APScheduler cron: morning_run / midday_refresh
#   3. API server         — FastAPI + SPA dashboard on :8002
#
# Idempotent: re-running never double-starts a process. Safe to run from a
# double-clickable .app, a Login Item, or the terminal. Runs in the user's
# session context so it has ~/Documents access (unlike a launchd daemon).

set -u

ROOT="/Users/thom/Documents/Personal/Code Projects/Macro Analyzer"
UV="/Users/thom/.local/bin/uv"
LOGDIR="$HOME/Library/Logs"
CHANNELS="feather_hands_trading,gem_hunters,og_whales,the_wolf_pack,ari_gold,trading_operation_desk,stock_unlocked"
PORT=8001   # canonical dashboard port — matches .claude/launch.json; do not change

mkdir -p "$LOGDIR"
cd "$ROOT" || { echo "FATAL: cannot cd into $ROOT (Documents access?)"; exit 1; }

started=""
skipped=""

start_if_absent() {
  # $1 = human name, $2 = pgrep pattern, $3 = command (run via nohup)
  local name="$1" pattern="$2" cmd="$3"
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    skipped="$skipped $name"
  else
    nohup bash -c "cd '$ROOT' && exec $cmd" >>"$LOGDIR/macro-${name}.log" 2>&1 &
    started="$started $name"
    sleep 1
  fi
}

# NOTE ON AUTOMATION: the collectors are managed by launchd, not this script:
#   - com.macro-analyzer.tg-listener  (KeepAlive listener)
#   - com.macro.free-ingest           (06:00 + 13:00 free ingest/scoring, no paid LLM)
# Both run `.venv/bin/python` directly (the interpreter granted Full Disk Access),
# so they survive reboot/sleep on their own. This script only ensures the
# listener is up (in case launchd is unloaded) and starts the dashboard server,
# which is NOT a launchd job. Paid LLM extraction stays a manual, budgeted step.

# 1. Telegram listener (launchd normally owns this; ensure it's up regardless)
start_if_absent "listener" "start_telegram_listener.py" \
  "$UV run python scripts/start_telegram_listener.py --channels $CHANNELS"

# 2. API server + dashboard (guard on the port, not just the pattern)
if lsof -ti ":$PORT" >/dev/null 2>&1; then
  skipped="$skipped api-server"
else
  nohup bash -c "cd '$ROOT' && exec $UV run uvicorn macro_positioning.api.main:app --host 127.0.0.1 --port $PORT" \
    >>"$LOGDIR/macro-api.log" 2>&1 &
  started="$started api-server"
  sleep 2
fi

echo "Macro Analyzer — run all"
echo "  started:${started:-  (none)}"
echo "  already running:${skipped:-  (none)}"
echo "  dashboard: http://127.0.0.1:$PORT/"
echo "  logs: $LOGDIR/macro-*.log"
