#!/usr/bin/env sh
set -eu

if [ -n "${PUID:-}" ] || [ -n "${PGID:-}" ]; then
  if [ -z "${PUID:-}" ] || [ -z "${PGID:-}" ]; then
    echo "Both PUID and PGID must be set together." >&2
    exit 1
  fi

  case "${PUID}" in
    ''|*[!0-9]*)
      echo "PUID must be a numeric value." >&2
      exit 1
      ;;
  esac
  case "${PGID}" in
    ''|*[!0-9]*)
      echo "PGID must be a numeric value." >&2
      exit 1
      ;;
  esac

  mkdir -p /app/data /app/.cache
  chown -R "${PUID}:${PGID}" /app/data /app/.cache

  export HOME=/app
  exec gosu "${PUID}:${PGID}" "$@"
fi

exec "$@"
