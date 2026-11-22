#!/bin/sh
set -eu

base=$1
head=$2
level=$3
case "$level" in
    docs|full) ;;
    *) echo 'Invalid CI level: expected docs or full' >&2; exit 1 ;;
esac

if [ -z "$base" ]; then
    level=full
elif git diff --quiet --no-renames "$base...$head" -- 'scripts/ci_*' .github pyproject.toml uv.lock; then
    :
else
    status=$?
    # git diff --quiet returns 1 for a difference. Propagate Git errors.
    [ "$status" -eq 1 ] || exit "$status"
    level=full
fi
printf '%s\n' "$level"
