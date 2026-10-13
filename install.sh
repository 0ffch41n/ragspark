#!/usr/bin/env bash
# RAGSpark installer. Run it from a clone of the repository:
#
#   sudo ./install.sh                  install (asks a few questions)
#   sudo ./install.sh --yes            install with the defaults
#   sudo ./install.sh check            only check the system
#
# After installation the same tool is available as `ragspark`
# (sudo ragspark status | start | stop | logs | uninstall ...).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "install: python3 is required / нужен python3" >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "install: Python 3.10 or newer is required / нужен Python 3.10 или новее" >&2
    exit 1
fi

# No command, or only options: install. -h/--help shows every command.
case "${1:-}" in
    -h|--help) ;;
    ""|-*) set -- install "$@" ;;
esac
exec python3 "$HERE/installer/ragspark.py" "$@"
