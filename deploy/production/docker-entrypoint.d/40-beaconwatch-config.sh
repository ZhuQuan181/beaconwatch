#!/bin/sh
set -eu

token_file="${BW_VIEWER_API_TOKEN_FILE:-/run/secrets/viewer_api_token}"
if [ ! -r "${token_file}" ]; then
  echo "viewer API token secret is missing: ${token_file}" >&2
  exit 1
fi

token="$(cat "${token_file}")"
case "${token}" in
  ""|*[!A-Za-z0-9._~-]*)
    echo "viewer API token contains unsupported characters" >&2
    exit 1
    ;;
esac

umask 022
printf 'window.__BEACONWATCH_CONFIG__ = {"apiToken":"%s"};\n' \
  "${token}" > /usr/share/nginx/html/config.js
