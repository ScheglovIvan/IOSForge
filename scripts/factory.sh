#!/bin/zsh
# Local IOSForge factory on a Mac: the admin plus the four Celery workers.
# Infra (Postgres/Redis/MinIO) is docker compose; see docs/local-setup-mac.md.
#
#   scripts/factory.sh start | stop | restart | status | logs [name]
#
# Paths default to this checkout; override with env vars if needed:
#   FACTORY_ROOT  repo checkout to run from     (default: the repo holding this script)
#   FACTORY_LOGS  pid + log directory           (default: $FACTORY_ROOT/var/factory)
#   ADMIN_HOST    admin bind address            (default: 127.0.0.1)
#   ADMIN_PORT    admin port                    (default: 8070)
set -e
ROOT=${FACTORY_ROOT:-${0:A:h:h}}
LOGS=${FACTORY_LOGS:-$ROOT/var/factory}
ADMIN_HOST=${ADMIN_HOST:-127.0.0.1}
ADMIN_PORT=${ADMIN_PORT:-8070}
NAMES=(intake codegen delivery xcode admin)
mkdir -p $LOGS
cd $ROOT

preflight() {
  local missing=()
  for bin in uv docker claude xcodegen xcrun ffmpeg; do
    command -v $bin >/dev/null || missing+=$bin
  done
  (( ${#missing} )) && echo "warning: not on PATH: ${missing[*]} (see docs/local-setup-mac.md)"
  [[ -f .env ]] || { echo "error: $ROOT/.env is missing (cp .env.example .env)"; exit 1; }
}

start_one() {
  local name=$1; shift
  if [[ -f $LOGS/$name.pid ]] && kill -0 $(cat $LOGS/$name.pid) 2>/dev/null; then
    echo "$name already running ($(cat $LOGS/$name.pid))"; return
  fi
  nohup "$@" >> $LOGS/$name.log 2>&1 &
  echo $! > $LOGS/$name.pid
  echo "$name started ($!)"
}

start() {
  preflight
  local celery="uv run celery -A iosforge.common.queue worker --loglevel=INFO"
  start_one intake   ${=celery} -Q discovery,acquisition,walkthrough --concurrency=1 -n intake@%h
  start_one codegen  ${=celery} -Q codegen  --concurrency=1 -n codegen@%h
  start_one delivery ${=celery} -Q delivery --concurrency=1 -n delivery@%h
  start_one xcode    ${=celery} -Q xcode    --concurrency=1 -n xcode@%h
  start_one admin    uv run uvicorn iosforge.admin.app:app --host $ADMIN_HOST --port $ADMIN_PORT
  echo "admin: http://$ADMIN_HOST:$ADMIN_PORT  (logs: $LOGS)"
}

stop() {
  local pids=()
  for name in $NAMES; do
    local f=$LOGS/$name.pid
    [[ -f $f ]] || continue
    local pid=$(cat $f)
    kill $pid 2>/dev/null && echo "stopping $name ($pid)" && pids+=$pid
    rm -f $f
  done
  # Celery warm shutdown finishes the running task first; wait so restart never doubles up.
  for pid in $pids; do
    while kill -0 $pid 2>/dev/null; do sleep 1; done
  done
  (( ${#pids} )) && echo "stopped" || true
}

case ${1:-start} in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status)
    for name in $NAMES; do
      if [[ -f $LOGS/$name.pid ]] && kill -0 $(cat $LOGS/$name.pid) 2>/dev/null; then
        echo "$name up ($(cat $LOGS/$name.pid))"
      else
        echo "$name DOWN"
      fi
    done ;;
  logs) tail -n 50 -f $LOGS/${2:-admin}.log ;;
  *) echo "usage: $0 start|stop|restart|status|logs [name]"; exit 2 ;;
esac
