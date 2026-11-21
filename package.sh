#!/usr/bin/env bash
# Assemble a submission folder: <dest>/{corpus (already there), code/, README.txt}
# Usage: bash package.sh <attempt-dir> <README.txt>
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$(mkdir -p "$1" && cd "$1" && pwd)"; README="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
rm -rf "$DEST/code"
mkdir -p "$DEST/code"
# source only: no caches, no private state
(cd "$HERE" && find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 | \
   xargs -0 -I{} cp --parents {} "$DEST/code/")
cp --remove-destination "$README" "$DEST/README.txt"
echo "code files: $(find "$DEST/code" -type f | wc -l)"
du -sh "$DEST/code"
