#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
bash "$ROOT/script/linux/stop.sh"
bash "$ROOT/script/linux/start.sh"
