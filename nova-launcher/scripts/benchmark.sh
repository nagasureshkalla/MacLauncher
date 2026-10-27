#!/usr/bin/env bash
# Wrapper: see scripts/benchmark.py
exec python3 "$(dirname "$0")/benchmark.py" "$@"
