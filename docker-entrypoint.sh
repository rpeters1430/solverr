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

  # Keep the large, immutable Camoufox browser tree owned by the image user.
  # Recursively chowning it on every NAS restart forces overlayfs to copy the
  # browser into the writable layer. Only runtime-writable paths need the
  # requested NAS ownership.
  mkdir -p /app/data /app/home /app/.cache/camoufox/tmp /app/.cache/camoufox/fontconfig
  chown -R "${PUID}:${PGID}" /app/data /app/home \
    /app/.cache/camoufox/tmp /app/.cache/camoufox/fontconfig

  # gosu derives $HOME from the target UID's /etc/passwd entry, ignoring any
  # HOME already exported here - an arbitrary NAS PUID with no passwd entry
  # falls back to HOME=/, which is read-only for a non-root user and breaks
  # Chromium/Camoufox (both need to write config/cache under $HOME). Register
  # a matching passwd/group entry so gosu resolves HOME to /app instead.
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
