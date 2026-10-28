#!/bin/sh
set -eu

if [ -z "${SOLVERR_PROXY_URL:-}" ]; then
  echo "Set SOLVERR_PROXY_URL, for example http://USER:PASSWORD@solverr:8192." >&2
  exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$script_dir/install_proxy_ca.sh"

exec yt-dlp \
  --proxy "$SOLVERR_PROXY_URL" \
  --compat-options no-certifi \
  "$@"
