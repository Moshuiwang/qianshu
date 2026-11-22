#!/bin/sh
set -eu

base=$1
head=$2
guard=$(mktemp "$RUNNER_TEMP/ci_guard.XXXXXX")
trap 'rm -f "$guard"' EXIT

if [ -z "$base" ] || ! git show "$base:scripts/ci_guard.sh" > "$guard"; then
    printf 'full\n'
    exit 0
fi

proposed=$(python3 scripts/ci_level.py "$base" "$head")
sh "$guard" "$base" "$head" "$proposed"
