# Machina CAD De-ID pipeline

Our entry for the **Machina CAD De-ID** track of the Builders Buildathon (2026).

The task: de-identify a raw engineering corpus (139.7 GB, 188,722 files: STEP, IGES,
SolidWorks, Creo, Inventor, Solid Edge, DWG/DXF, PDF drawings, FreeCAD / 3MF / Fusion
archives, meshes, images and nested archives) by replacing every identifying name,
account, domain, host and project name with a **consistent pseudonym**, while every
file stays a usable native file and the links between drawings, parts and assemblies
keep working.

This repository contains the pipeline source only. It contains **no corpus data, no
transformed output, no identity dictionary, no pseudonym key and no run logs** (see
[What is not in this repository](#what-is-not-in-this-repository)).

## Design in one page

1. **Harvest, then decide, then write.** A first pass reads only the places where
   identities live (author / company / last-saved-by properties, user-name components
   of stored file-system paths, e-mail addresses, URLs, title-block text). All values
   of the whole corpus go into one global identity dictionary before a single byte is
   changed, so one identity gets one pseudonym everywhere.
2. **Pseudonyms keep the shape of the original.** A pseudonym is derived per word with
   HMAC-SHA256 from a private key; it has the same length, the same letter case and
   digit positions and is pronounceable. Punctuation and separators stay. Because the
   length is preserved, fixed-width records, length prefixes, stream sizes and file
   offsets of binary containers stay valid, and identity fields stay populated instead
   of being blanked.
3. **Only edit bytes the handler understands.** Each format has a handler that knows
   where text is allowed to change (string literals in STEP, counted strings in ACIS,
   text group codes in DXF, property-set strings in OLE files, length-prefixed strings
   in SolidWorks streams, ...). Geometry, numeric data and format keywords are never
   candidates. Unknown binary regions are left alone.
4. **Engineering vocabulary is protected.** A name that is also an ordinary word, a
   software product, a calendar word, an engineering term or a catalogue-part vendor is
   replaced only inside identity fields, or only in the source project where it was
   seen as an identity - never corpus-wide in free text.
5. **Every written file is checked before it is accepted.** The output is re-parsed
   with an independent reader and compared with the input (same streams, same sizes,
   same archive members, only the intended strings differ). On any doubt the original
   bytes are kept and the file is listed in the submission notes. No file is dropped.

## Passes

| Pass | Script | Output (private, in `$DEID_WORK`) |
|------|--------|-----------------------------------|
| 1. Harvest | `deid/harvest.py` | identity-field values per source group, file inventory |
| 1b. PDF text | `deid/pdfharvest.py`, `deid/textharvest.py` | title-block lines, logo candidates, identities completed from visible text |
| 2. Dictionary | `deid/build_dict.py` | identity dictionary + collision-checked pseudonym table |
| 2b. Reference protection | `deid/protect.py` | file-name stems that must stay intact when paths are not renamed |
| 3. Transform | `deid/transform.py` | the output corpus + a journal (one record per file) |
| 4. Census (optional) | `deid/census.py` | entries that behave like vocabulary, to demote |
| 5. Verify | `deid/verify.py` | completeness / structure / residual report |
| 6. Notes | `deid/report.py` | `README.txt` of the submission (run summary, exceptions) |

`run_all.sh` runs the whole sequence.

### Identity dictionary (`build_dict.py`)

* **Classes**: person, organisation, account, host, domain, project.
* **Tiers** decide where an entry may be replaced:
  * `A` - anywhere in eligible text, corpus-wide (long or unambiguous identities);
  * `L` - in free text only inside the source projects where it was seen as an
    identity, and in identity fields everywhere (first names, short accounts, names
    that are also ordinary words);
  * `B` - identity fields only (very short or ordinary-looking values, vendors);
  * `P` - protected file-name stems (kept where another file refers to them).
* **Alias resolution**: `DOMAIN\user (HOST)` forms, e-mail local parts, 8.3 short
  names, case and separator variants, squashed forms (`janedoe` = `Jane Doe`),
  initial + surname accounts (`jdoe` = `John Doe` within one project) all resolve to
  the same pseudonym letters.
* **Protection lists** (`deid/data/`): placeholder values, software names,
  engineering terms, structural words, calendar words, catalogue vendors, safe
  infrastructure domains, and ordinary-vocabulary lists for eight languages.
* **Collision checks**: two different identities never share a pseudonym, and a
  pseudonym is never an ordinary word.

### Matching engine (`engine.py`)

One Aho-Corasick automaton over a normalised view of the text (lower case, accents
folded), with UTF-8 / Latin-1 / GBK / UTF-16LE key variants for byte scans, word
boundary and camelCase rules, leftmost-longest selection and a final check on the
real characters. Files are memory-mapped and scanned in chunks.

### Format handlers (`handlers.py`, `fmt_*.py`)

| Format | What is edited | How integrity is kept |
|--------|----------------|-----------------------|
| STEP / IGES | header fields, PERSON / ORGANIZATION / ADDRESS entities, string literals, comments | only inside strings; IGES Hollerith fields are edited through an offset map, lengths unchanged |
| ACIS SAT, DXF, SVG, XML, G-code, OBJ ... | counted strings, text group codes, text nodes and attribute values, comments | format-aware eligibility rules, default deny; namespace URIs never touched |
| SolidWorks 2015+ | docProps XML, serialized strings, stored paths, sheet previews | each stream is re-deflated to exactly its original compressed size, CRC-32 fields updated - no offset moves |
| SolidWorks (OLE), Inventor, Solid Edge | property sets, serialized strings, zlib sub-streams | streams keep their size; rewritten with a second parser comparing every stream |
| Creo | revision records (user names), string values | same-length, NUL-terminated strings only |
| DWG R2004+ | metadata sections: summary information, application history, file dependency list | only literal bytes of the LZ77 page stream are edited; both page checksums recomputed; the page must decompress to the intended content; object and header sections (own CRC-16) are not touched |
| PDF | Info / XMP metadata, bookmarks, annotation authors, visible page text, repeated logo images | saved with pikepdf / PyMuPDF and re-opened; text redrawn at the same position |
| FreeCAD, 3MF, Fusion, ZIP-based | XML members and nested files | untouched members are copied byte for byte |
| Archives (zip, tar, gz, Z, 7z) | members, recursively | repacked with the same member list |
| Images | EXIF / XMP text tags, GPS data | pixels never change |

Formats that are not parsed (CATIA V5, JT, NX, DWG R2000 and older, RAR archives)
are passed through unchanged and listed in the notes.

### Path renaming (`paths.py`, `retree.py`)

With `transform.py --rename {owner,top,all}` each path component is passed through
the same engine and the same dictionary as file contents, so a file name and every
reference to it inside other files change in exactly the same way. Extensions, Creo
version suffixes, commit hashes and components without an identity are unchanged.
`refcheck.py` compares reference resolution before and after.

### Verification (`verify.py`)

* every input file has exactly one output file, nothing extra;
* files the journal reports as unchanged are byte-identical to the input;
* every changed file is re-opened with an independent parser (qpdf, olefile,
  zipfile, the SolidWorks container validator) and compared structurally;
* fixpoint: running the transform again on a changed file must find nothing left.

## Running it

Requirements: Ubuntu 24.04, Python 3.12.

```bash
sudo apt-get install -y python3-venv p7zip-full ncompress qpdf poppler-utils \
     wamerican wamerican-large wngerman wfrench wspanish witalian wcatalan wportuguese wdutch
python3 -m venv venv && venv/bin/pip install -r requirements.txt
bash tools/build_wordlists.sh            # generates deid/data/words_*.txt

export DEID_INPUT=/data/input            # raw corpus (read-only)
export DEID_WORK=/data/work              # private state: dictionary, key, journal
export DEID_OUT=/data/out/corpus         # output corpus
bash run_all.sh venv/bin/python          # DEID_RENAME=all for the renamed variant
bash package.sh /data/out /data/work/README.txt    # adds code/ and README.txt next to corpus/
```

The corpus is untrusted input: scripts are run with `python -I` and never from inside
the data directory. Individual passes can be run and re-run on their own, for example
`transform.py --paths list.txt` re-processes selected files, and `--prefix` / `--limit`
restrict a pass to part of the corpus for testing.

On an 8-vCPU machine the transform takes about 35 minutes for the whole corpus when
the disk delivers ~125 MB/s (reading the input is the bottleneck), and verification
about the same.

The pseudonym key (`$DEID_WORK/secret.key`) is generated on first use. With a
different key the same pipeline produces the same structure with different
pseudonyms.

## Repository layout

```
deid/               pipeline package (one module per pass, fmt_* per format)
deid/data/          protection lists; see deid/data/README.md
tools/              build_wordlists.sh
run_all.sh          end-to-end run
package.sh          assemble a submission folder (corpus/, code/, README.txt)
requirements.txt    exact Python environment of the runs
```

## What is not in this repository

* The competition corpus and any file derived from it (raw input, transformed
  output, archives, previews).
* The identity dictionary, the pseudonym key and table, journals and logs - these
  are the private mapping between original and pseudonymised identities.
* Three small hand-reviewed lists that contain original identifiers of the corpus
  (`deid/data/curated_identities.tsv`, `text_exclude.txt`, `repo_exclude.txt`). They
  ship with the submitted `code/` folder of each attempt, as the rules allow, but are
  not published here. The pipeline runs without them; `*.example` files show the
  format.
* The generated vocabulary lists (`deid/data/words_*.txt`); `tools/build_wordlists.sh`
  rebuilds them from Ubuntu dictionary packages.

## Known limitations

* Text drawn as vector outlines or contained in scanned / raster images is not read
  (no OCR); vector logos are not detected.
* Previews are coarsened for SolidWorks drawings only; other embedded previews are
  left as they are.
* Text entities inside DWG object data (title blocks drawn in the model) are not
  edited; DWG R2000 and older are passed through.
* Part numbers and project codes are replaced only where they coincide with a
  harvested identity.
* RAR archives and a few 7z archives cannot be re-created with free tools and are
  passed through.
