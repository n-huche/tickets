#!/usr/bin/env bash
# Supervisor do monitor (a box não tem systemd nem cron).
# - instância única via flock
# - reinicia o daemon se ele cair (backoff 10s..5min)
# - mata e reinicia o daemon se o heartbeat ficar parado > 5 min (travado)
# Iniciar/parar: ./tickets service start|stop|restart|status
set -u
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$DIR" || exit 1
mkdir -p data logs
exec 9>data/supervisor.lock
if ! flock -n 9; then echo "supervisor já está rodando"; exit 0; fi
echo $$ > data/supervisor.pid
LOG=logs/supervisor.log
STALE=300
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }
rotate() { for f in "$LOG" logs/daemon.out; do
  [[ -f "$f" && $(stat -c %s "$f") -gt 5000000 ]] && mv -f "$f" "$f.1"; done; }
child=""
term() { log "supervisor: SIGTERM, parando daemon pid=$child"; [[ -n "$child" ]] && kill -TERM "$child" 2>/dev/null
  for _ in $(seq 30); do kill -0 "$child" 2>/dev/null || break; sleep 1; done
  kill -9 "$child" 2>/dev/null; rm -f data/supervisor.pid data/daemon.pid; exit 0; }
trap term TERM INT
backoff=10
log "supervisor: iniciado pid=$$"
while true; do
  rotate
  start=$(date +%s)
  .venv/bin/python -m tickets_app.daemon >> logs/daemon.out 2>&1 9>&- &
  child=$!
  echo "$child" > data/daemon.pid
  log "supervisor: daemon iniciado pid=$child"
  while kill -0 "$child" 2>/dev/null; do
    sleep 30 9>&- & wait $!
    if [[ -f data/heartbeat.json ]]; then
      age=$(( $(date +%s) - $(stat -c %Y data/heartbeat.json) ))
      if (( age > STALE && $(date +%s) - start > STALE )); then
        log "supervisor: heartbeat parado há ${age}s -> daemon TRAVADO, reiniciando pid=$child"
        kill -TERM "$child" 2>/dev/null; sleep 15; kill -9 "$child" 2>/dev/null
      fi
    fi
  done
  wait "$child"; rc=$?
  ran=$(( $(date +%s) - start ))
  (( ran > 600 )) && backoff=10
  log "supervisor: daemon saiu rc=$rc após ${ran}s; reiniciando em ${backoff}s"
  sleep "$backoff" 9>&- & wait $!
  backoff=$(( backoff * 2 > 300 ? 300 : backoff * 2 ))
done
