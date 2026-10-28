#!/bin/sh
set -eu

# Container init helper for the opt-in Solverr HTTPS-inspection CA.
# Required: SOLVERR_PROXY_CA_URL and SOLVERR_PROXY_CA_SHA256 (PEM SHA-256 from Solverr logs).

if [ "${SOLVERR_PROXY_CA_ACTION:-install}" = "remove" ]; then
  rm -f /usr/local/share/ca-certificates/solverr-proxy-ca.crt
  update-ca-certificates
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Proxy CA installation must run as root in the client container." >&2
  exit 1
fi
if [ -z "${SOLVERR_PROXY_CA_URL:-}" ] || [ -z "${SOLVERR_PROXY_CA_SHA256:-}" ]; then
  echo "Set SOLVERR_PROXY_CA_URL and SOLVERR_PROXY_CA_SHA256 before installing the Solverr CA." >&2
  exit 1
fi

tmp_file=$(mktemp)
trap 'rm -f "$tmp_file"' EXIT HUP INT TERM
if command -v curl >/dev/null 2>&1; then
  curl --fail --silent --show-error --location "$SOLVERR_PROXY_CA_URL" --output "$tmp_file"
elif command -v wget >/dev/null 2>&1; then
  wget -q "$SOLVERR_PROXY_CA_URL" -O "$tmp_file"
else
  echo "Install curl or wget in the client container to download the Solverr CA." >&2
  exit 1
fi

actual=$(sha256sum "$tmp_file" | awk '{print tolower($1)}')
expected=$(printf '%s' "$SOLVERR_PROXY_CA_SHA256" | tr '[:upper:]' '[:lower:]' | tr -d ': ')
if [ "$actual" != "$expected" ]; then
  echo "Solverr CA fingerprint mismatch; refusing to install." >&2
  echo "Expected: $expected" >&2
  echo "Received: $actual" >&2
  exit 1
fi

mkdir -p /usr/local/share/ca-certificates
install -m 0644 "$tmp_file" /usr/local/share/ca-certificates/solverr-proxy-ca.crt
update-ca-certificates
echo "Installed Solverr proxy CA ($actual)."
