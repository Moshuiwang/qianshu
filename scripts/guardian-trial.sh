#!/bin/sh
set -eu
usage() {
    echo '用法：guardian-trial.sh --state DB --list-recovery | --state DB --trial-reconcile SCOPE GEN VALIDITY NEXT [--retry-of ID]' >&2
    exit 2
}
[ "${1-}" = --state ] || usage
case "${3-}" in
    --list-recovery) [ "$#" -eq 3 ] || usage ;;
    --trial-reconcile)
        [ "$#" -eq 7 ] || { [ "$#" -eq 9 ] && [ "$8" = --retry-of ]; } || usage ;;
    *) usage ;;
esac
root=$(CDPATH='' cd -- "$(/usr/bin/dirname -- "$0")/.." && pwd)
exec /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 "$root/.venv/bin/python" -I -m qianshu "$@"
