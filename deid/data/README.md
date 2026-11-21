# Data files

Hand-written protection lists (plain text, `#` starts a comment):

| File | Purpose |
|------|---------|
| `generic_values.txt` | placeholder values that are never identities ("Administrator", "user", "unknown", 8.3 folder names ...) |
| `software.txt` | CAD / office / OS product names |
| `engineering_terms.txt` | engineering vocabulary and standards bodies |
| `structural_words.txt` | words kept inside organisation names ("university", "gmbh", "labs", TLD words) |
| `calendar_words.txt` | month and weekday names in several languages |
| `vendors.txt` | catalogue-part vendors: replaced in identity fields only |
| `safe_domains.txt` | infrastructure domains (mail providers, code forges, publishers, CDNs) |
| `limits_paths_*.txt` | "limitations" section of the submission notes |

Generated lists (not in the public repository; run `tools/build_wordlists.sh`):

| File | Source |
|------|--------|
| `words_en.txt`, `words_proper.txt` | Ubuntu `wamerican` |
| `words_en_large.txt` | Ubuntu `wamerican-large` |
| `words_foreign.txt` | Ubuntu `wngerman`, `wfrench`, `wspanish`, `witalian`, `wcatalan`, `wportuguese`, `wdutch` |

Corpus-specific reviewed lists (not in the public repository because they contain
original identifiers; the pipeline runs without them):

| File | Format |
|------|--------|
| `curated_identities.tsv` | `path-prefix <TAB> PERSON\|ORG\|PROJECT\|DOMAIN <TAB> name` - identities known from folder names or visible text, replaced inside the given path scope |
| `text_exclude.txt` | one value per line: text-derived candidates that are not identities |
| `repo_exclude.txt` | whitespace-separated repository-name words that describe content rather than name a project |

See the `*.example` files.
