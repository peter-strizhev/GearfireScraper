#!/bin/sh
set -eu

Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp &
xvfb_pid=$!
trap 'kill "$xvfb_pid" 2>/dev/null || true' EXIT INT TERM
export DISPLAY=:99

exec "$@"
