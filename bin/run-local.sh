#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m image_harvester "$@"
