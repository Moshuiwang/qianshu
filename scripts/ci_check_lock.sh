#!/bin/sh
set -eu
for manifest in pyproject.toml uv.lock; do
    if [ ! -f "$manifest" ]; then
        echo "完整档缺少 $manifest" >&2
        exit 1
    fi
done
uv lock --check --managed-python --python "$(cat .python-version)"
