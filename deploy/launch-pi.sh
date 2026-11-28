#!/bin/sh
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python=${ELI_PI_PYTHON:-"$repo_dir/.venv/bin/python"}
if [ ! -x "$python" ]; then
    printf 'Eli Python is unavailable: %s\nSet ELI_PI_PYTHON to the existing venv Python.\n' "$python" >&2
    exit 2
fi
cd "$repo_dir"
exec "$python" -u -m deploy.pi "$@"
