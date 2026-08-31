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

  mkdir -p /app/data /app/.cache
  chown -R "${PUID}:${PGID}" /app/data /app/.cache

  # gosu derives $HOME from the target UID's /etc/passwd entry, ignoring any
  # HOME already exported here - an arbitrary NAS PUID with no passwd entry
  # falls back to HOME=/, which is read-only for a non-root user and breaks
  # Chromium/Camoufox (both need to write config/cache under $HOME). Register
  # a matching passwd/group entry so gosu resolves HOME to /app instead.
  if ! getent passwd "${PUID}" >/dev/null 2>&1; then
    echo "solverr:x:${PUID}:${PGID}:solverr:/app:/bin/sh" >> /etc/passwd
  fi
  if ! getent group "${PGID}" >/dev/null 2>&1; then
    echo "solverr:x:${PGID}:" >> /etc/group
  fi

  exec gosu "${PUID}:${PGID}" "$@"
fi

exec "$@"
