#!/usr/bin/env bash
# End-to-end run of the de-identification pipeline.
#
#   DEID_INPUT   raw corpus (read-only)                default ~/machina/input
#   DEID_WORK    private state (dictionary, key, ...)   default ~/machina/work
#   DEID_OUT     output corpus directory                default ~/machina/out/corpus
#   DEID_JOBS    worker processes                       default 8
#   DEID_RENAME  path renaming: "" (none) | owner | top | all      default ""
#
# Usage: bash run_all.sh [/path/to/venv/bin/python]
#
# Everything written to DEID_WORK (identity dictionary, pseudonym key, journal,
# logs) is private and must never be published or submitted.
set -euo pipefail
PY="${1:-python3}"
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${DEID_WORK:=$HOME/machina/work}"
: "${DEID_OUT:=$HOME/machina/out/corpus}"
: "${DEID_JOBS:=8}"
: "${DEID_RENAME:=}"
export DEID_WORK DEID_OUT
J="$DEID_WORK/journal.jsonl"
D="$DEID_WORK/dict"
mkdir -p "$DEID_WORK"

# 0. ordinary-vocabulary lists (generated from Ubuntu dictionary packages)
[ -f "$HERE/deid/data/words_en.txt" ] || bash "$HERE/tools/build_wordlists.sh"

# 1. harvest: identity fields of every file (archives are opened recursively),
#    then the visible text lines and repeated small images (logos) of PDFs
"$PY" -I "$HERE/deid/harvest.py" --jobs "$DEID_JOBS"
"$PY" -I "$HERE/deid/pdfharvest.py" --jobs "$DEID_JOBS"

# 2. dictionary: from metadata first, then completed with identities read from
#    drawing text (which needs the first dictionary), then the final build
#    The final build reads the corpus manifest (path, bytes, md5; default: next to
#    the input folder) so that exact copies of a file are treated alike.
"$PY" -I "$HERE/deid/build_dict.py" --no-text --out "$D"
"$PY" -I "$HERE/deid/textharvest.py" --dict "$D/dict.json"
"$PY" -I "$HERE/deid/build_dict.py" --out "$D" ${DEID_MANIFEST:+--manifest "$DEID_MANIFEST"}

FLAGS=(--scope --pdftext --pdfextra --logos "$DEID_WORK/pdflogos.json" --previews --creo --zlb --strings --dwg --dwgobj)
LIMITS="$HERE/deid/data/limits_paths_unchanged.txt"
if [ "$DEID_RENAME" = "all" ]; then
  FLAGS+=(--rename all)
  LIMITS="$HERE/deid/data/limits_paths_renamed.txt"
else
  # file names stay (at least partly) unchanged: keep references to them intact
  "$PY" -I "$HERE/deid/protect.py" --dict "$D/dict.json"
  [ -z "$DEID_RENAME" ] || FLAGS+=(--rename "$DEID_RENAME")
fi

# 3. transform: one output file per input file; unchanged files are hard links
"$PY" -I "$HERE/deid/transform.py" --out "$DEID_OUT" --dict "$D/dict.json" --journal "$J" \
     --jobs "$DEID_JOBS" "${FLAGS[@]}"

# 4. verify: completeness, byte identity of untouched files, structure of every
#    changed file, residual scan (re-processing a changed file must find nothing)
"$PY" -I "$HERE/deid/verify.py" --out "$DEID_OUT" --dict "$D/dict.json" --journal "$J" \
     --jobs "$DEID_JOBS" --report "$DEID_WORK/verify.jsonl" "${FLAGS[@]}"

# 5. submission notes (run summary, per-file exceptions with final paths)
"$PY" -I "$HERE/deid/report.py" --journal "$J" --out "$DEID_WORK/README.txt" --limits "$LIMITS"
echo "done: corpus in $DEID_OUT, notes in $DEID_WORK/README.txt"
