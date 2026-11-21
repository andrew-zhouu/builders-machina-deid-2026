#!/usr/bin/env bash
# Regenerate the ordinary-vocabulary lists in deid/data/ from Ubuntu dictionary
# packages. The lists protect ordinary words from being treated as identities.
#
#   sudo apt-get install -y wamerican wamerican-large wngerman wfrench wspanish \
#        witalian wcatalan wportuguese wdutch
#   bash tools/build_wordlists.sh [output-dir]
#
# Output (lower case, one word per line):
#   words_en.txt        common English words          (wamerican)
#   words_proper.txt    capitalised English entries   (wamerican: names, places)
#   words_en_large.txt  large English list            (wamerican-large)
#   words_foreign.txt   de/fr/es/it/ca/pt/nl words, accent-folded to ASCII
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/../deid/data}"
D=/usr/share/dict
export LC_ALL=C
for f in american-english american-english-large ngerman french spanish italian catalan portuguese dutch; do
  [ -r "$D/$f" ] || { echo "missing $D/$f (install the w* packages listed above)" >&2; exit 1; }
done
mkdir -p "$OUT"
grep -v "'" "$D/american-english" | grep -v '^[A-Z]' | tr 'A-Z' 'a-z' | sort -u > "$OUT/words_en.txt"
grep '^[A-Z]' "$D/american-english" | grep -v "'" | tr 'A-Z' 'a-z' | sort -u > "$OUT/words_proper.txt"
grep -v "'" "$D/american-english-large" | tr 'A-Z' 'a-z' | grep -E '^[a-z]{3,}$' | sort -u > "$OUT/words_en_large.txt"
python3 -I - "$OUT/words_foreign.txt" <<'PY'
import sys, unicodedata
out = set()
for name in ("ngerman", "french", "spanish", "italian", "catalan", "portuguese", "dutch"):
    p = "/usr/share/dict/" + name
    for enc in ("utf-8", "latin-1"):
        try:
            txt = open(p, encoding=enc).read()
            break
        except UnicodeDecodeError:
            continue
    for w in txt.split("\n"):
        w = w.strip().lower()
        if not w or "'" in w or len(w) < 3:
            continue
        c = "".join(ch for ch in unicodedata.normalize("NFKD", w) if not unicodedata.combining(ch)).replace("ß", "s")
        if c.isalpha() and c.isascii():
            out.add(c)
open(sys.argv[1], "w").write("\n".join(sorted(out)) + "\n")
PY
wc -l "$OUT"/words_*.txt
