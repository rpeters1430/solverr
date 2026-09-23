#!/usr/bin/env sh
set -eu

# Ensure files written to bind mounts (/app/data) are group/world readable on host
umask 0002

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

  # Chowning the browser tree would make overlayfs copy it into the writable layer on every start.
  mkdir -p /app/data /app/home /app/.cache/camoufox/tmp /app/.cache/camoufox/fontconfig
  chown -R "${PUID}:${PGID}" /app/data /app/home \
    /app/.cache/camoufox/tmp /app/.cache/camoufox/fontconfig

  # gosu takes $HOME from /etc/passwd; an unknown UID gets a read-only HOME=/ and breaks Camoufox.
  if ! getent passwd "${PUID}" >/dev/null 2>&1; then
    echo "solverr:x:${PUID}:${PGID}:solverr:/app/home:/bin/sh" >> /etc/passwd
  fi
  if ! getent group "${PGID}" >/dev/null 2>&1; then
    echo "solverr:x:${PGID}:" >> /etc/group
  fi

  export HOME=/app/home
  exec gosu "${PUID}:${PGID}" "$@"
fi

exec "$@"
