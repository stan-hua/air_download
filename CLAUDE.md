# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CLI + Python library wrapping the AIR (Automated Image Retrieval) portal web API, used to batch-download radiology studies (DICOM, delivered as zip archives) from a PACS system. The upstream API contract is vendored at `docs/air_open_api.yaml` — consult it before changing request payloads or adding endpoints.

## Commands

pixi is the only supported package manager — never `pip install` into the environment or invoke a bare `python`/`pytest`.

```bash
pixi install                   # solve + create the default environment
pixi run test                  # run all tests
pixi run pytest tests/test_filters.py::test_name -v   # single test
pixi run download -h           # CLI entry point (air_download.cli:cli)
pixi add <pkg>                 # conda dep; --pypi only if unavailable on conda-forge
```

Tasks mirror the workflows in README.md — `download`, `search`, `list-projects`, `list-profiles`, `match`, `download-cohort`, `frames`, `test`. They are thin wrappers over `air_download` with a flag pre-applied, and pixi appends trailing arguments, so keep them in sync when CLI flags change.

The package is installed editable into the environment, so source edits need no reinstall. `pixi.lock` is committed; `.pixi/` is not.

There is no linter or formatter configured. Existing code in `air_download/` uses Google-style docstrings and full type hints, which predates the conventions below — follow **Conventions** for new code.

## Architecture

Request flow for every operation is: `cli.py` parses args → constructs `AIRClient` → calls `client.download(...)` (which internally calls `client.search(...)`) → `filters.py` narrows results → `utils.py` builds output paths / writes CSV.

- **`client.py`** — `AIRClient`, the whole API surface. Holds a `requests.Session`, a lazily-acquired JWT (`_auth_header` property authenticates on first use, so callers never call `authenticate()` explicitly), and the project list cached from the login response.
- **`filters.py`** — `apply_inclusion_filter` / `apply_exclusion_filter`. Both take a comma-separated pattern string and do case-insensitive substring matching with OR logic over one dict field. They are applied to *exams* (on `modality` / `description`) inside `search()`, and to *series* (on `description`) inside `_download_single_exam`. Inclusion runs before exclusion. Also holds the `--thinnest-axial` series selection (see below).
- **`utils.py`** — `build_exam_output_path` (directory vs. `.zip` path disambiguation, index suffix to avoid overwriting) and `write_exams_csv` (appends to `<output>/accessions.csv`, header written only when the file is new).
- **`cli.py`** — arg parsing, logging setup, table printing. `_configure_logging` deliberately only touches the `air_download` logger so `urllib3`/`requests` stay quiet.
- **`crosswalk.py`** — the mapping from real MRNs and accession numbers to the pseudonyms every output path uses. Top level, not under `us_ct/`, because it names neither modality and because `frames.py` imports it — putting it in the subpackage would invert the dependency direction. A library module with no CLI, so no `fire.Fire()`.
- **`us_ct/`** — the subpackage holding everything that assumes an ultrasound→CT pairing specifically. The boundary is the point: a module belongs here if it names `us`/`ct` in its flags, columns, or output layout, and outside it if it generalises. `frames.py` and `crosswalk.py` are deliberately outside, since frame counting and pseudonymisation apply to any downloaded DICOM. Don't add general-purpose code here, and don't teach these two modules a third modality — a general matcher would be a new module, not a widening of these.
  - **`us_ct/match.py`** — cohort building, not API access: pairs two search-result CSVs into ultrasound→CT matches. Its own `air_match` entry point, and the first module written to the **Conventions** below (`fire.Fire()`, NumPy docstrings, grouped imports), since it was added after they were set. New modules should follow it rather than the older files.
  - **`us_ct/cohort.py`** — downloads the paired CSV `match.py` writes, into `<output>/P0001/visit-01/{us,ct}/A0001.zip`. Own `air_cohort` entry point. Pure orchestration over `AIRClient.download()`: it passes an explicit `.zip` path per exam, which works only because `build_exam_output_path` returns a non-existent `.zip` path verbatim — and only that function's *directory* branch calls `mkdir`, so `cohort.py` creates the parents itself. Follows the **Conventions**, like `match.py`.
- **`frames.py`** — counts frames in downloaded DICOM files and prunes by them. Own `air_frames` entry point. The only module that reads local files rather than the API.
- **`air_download/air_download.py`** — backward-compat shim re-exporting the public names. Add new code to the focused modules, not here.

### Download protocol quirks

`_download_single_exam` implements a three-step server handshake that must stay in this order: `download/start` → poll `download/check` until status is `started`/`completed` → stream `download/zip`.

- `download/start` is posted with `raise_for_status=False` because the server returns non-2xx bodies carrying actionable JSON (`reason` mentioning "project" or "profile"); `_check_download_started` parses that and prints the valid project/profile lists before raising.
- `download/zip` is the one call that does **not** use the `Authorization` header — the JWT goes in the form body as `jwt`, per the API.
- The exam dict returned from search is passed back to the server verbatim as the `study` payload. Do not reshape it; `search()` only pops `patientName` (deliberately, to avoid carrying PHI through the pipeline).

### Two layers of narrowing

Keep these distinct — conflating them is the easiest mistake to make in this codebase:

- **Server-side query parameters** (`modality`, `study_description`, plus `accession`/`mrn`) go into the `query-data-source` payload and decide what the data source returns at all. `modality` must be a single code from `MODALITIES` in `client.py` (mirrored from the spec's enum) and is validated by `normalize_modality` before any network call. A search needs at least one of these four.
- **Client-side filters** (`*_inclusion` / `*_exclusion`, CLI `-xm`/`-xd`/`-s`) run in `filters.py` after results return and are pure substring matching.

The spec marks `name`, `mrn`, `accNum`, `studyUid`, `studyDescription`, `modality`, `sourceId`, and `dateRange` as required in the query payload — send all of them, empty-string the unused ones.

### Date chunking

The data source caps results per query, so `search()` never issues one request. `build_date_ranges` (in `utils.py`, pure and injectable via `now=`) turns the requested window into a list of `dateRange` payloads of at most `chunk_days` (default 7); `_search_date_ranges` queries each and merges. Consequences to preserve when touching this path:

- Chunk boundaries **touch** (`chunk[n].end == chunk[n+1].start`), so an exam on a boundary comes back twice. De-duplication via `exam_key` (study UID, falling back to accession + dateTime) is what makes that safe — don't drop it.
- Client-side filters run **once after** merging, not per chunk.
- `build_date_ranges` always returns at least one element; all-empty strings means "no date restriction", which is how the un-dated path stays uniform.
- The response's `truncated` flag is checked per chunk and warns with that chunk's dates, since a 7-day window can still overflow for a busy modality.
- Naive datetimes get the local offset attached, because every datetime format the spec accepts carries one (only bare `yyyy-MM-dd` may omit it).

### Series selection (`--thinnest-axial`)

What the API gives you per series is only `description`, `imageCount`, `modality`, `seriesNumber`, `seriesUid` — **no slice thickness and no plane**. That asymmetry drives the whole design in `filters.py`:

- The selection keeps **exactly one series or none** — the thinnest axial reconstruction. Structured reports are *not* kept: they were until 2026-08, and were dropped deliberately because they carry no image data. Don't reinstate them.
- Axial detection and thickness are heuristics over `description`, and are therefore configurable and loudly logged. Don't present them as reliable.

Details worth preserving: axial patterns match as **whole words** (`\b…\b`) specifically so `THORAX` and `TRAUMA` don't register as axial — plain substring matching, as used elsewhere in this module, would break that. `parse_slice_thickness` bounds values to 0.1–20mm so a field of view like `FOV 512MM` isn't mistaken for a thickness. When no axial series states a thickness, selection falls back to max `imageCount` and says so at INFO, because that proxy is wrong when candidate series cover different anatomy. Candidates are series whose modality is `CT` or absent; the absent case exists so a data source that omits the field doesn't select nothing.

Selection runs *after* `-s`/`-s-exclude` in `_download_single_exam`, and returns **empty** (with a warning naming the consequence) rather than everything when no axial matches — silently downloading a whole study would be worse than downloading nothing. Empty means `_download_single_exam` writes no archive, which `cohort.py` then counts as a failed exam; that chain is intentional, so a miss is visible rather than silent.

`keep_thinnest_axial` is the public entry point (returns a list); `select_thinnest_axial` is the picker (returns one series or None), kept separate so a caller that only wants the decision does not trigger the wrapper's warning.

### Bulk input (`--accessions-csv`)

`read_accession_pairs` (in `utils.py`) reads **(mrn, accession_number) pairs, not accessions**. That pairing is a correctness requirement, not convenience: the same accession number can belong to different patients, so querying on the accession alone can return the wrong patient's exam. Never "simplify" this to a list of accessions.

Rows missing either field are skipped with a warning rather than queried on a partial key; exact duplicate pairs are collapsed, which matters because `write_exams_csv` appends and re-running a search doubles every row. `cli.py` splits into `_run_from_csv` (drives its own progress bar, merges results and writes the CSV once under `--search-only`) and `_run_single_query`. The per-exam bar in `download()` is disabled for single-exam results so a 25k-row CSV doesn't leave 25k bars behind.

### Cohort matching (`us_ct/match.py`)

Three rules define a qualifying pair, and each has a test pinning it: same patient **by MRN** (never by accession, which repeats across patients), CT **strictly after** the ultrasound (equal timestamps do not qualify — neither followed the other), and within `max_hours` **inclusive** at the boundary. Comparisons use timezone-aware datetimes so mixed offsets and midnight crossings are correct; don't reduce them to naive dates.

Default keeps the earliest qualifying CT per ultrasound; `--all_pairs` emits all. Output is overwritten rather than appended, deliberately unlike `write_exams_csv`. Log counts only — never MRNs or accession numbers.

`us_image_count` / `ct_image_count` are carried through from the `image_count` column of the search CSV via `.get(..., "")`, so a CSV predating that column still matches — don't promote it into `_REQUIRED_COLUMNS`.

`select_one_us_per_ct` (`--us_selection`) is a pure post-filter over emitted rows, deliberately *not* folded into `match_exams`, which stays untouched along with the tests pinning it. `closest` takes the highest `us_rank_before_ct` within each `(mrn, ct_accession_number)` group rather than filtering `is_closest_us == True`: that flag is computed over every ultrasound the patient has, so filtering on it couples the result to an invariant this function doesn't control, while taking the max rank guarantees exactly one survivor per CT unconditionally. A `most_images` strategy ranking on `us_image_count` existed and was removed: that counts DICOM objects, not frames, so a single-view study saved as many stills outranks a multi-clip FAST. Don't reintroduce it — frame counts are the only honest signal and they exist only after downloading (`frames.py`). The `us_image_count` / `ct_image_count` columns stay as raw data, but nothing selects on them.

### Frame counting (`frames.py`)

The one module that reads local DICOM files rather than the web API — a deliberate widening of the package's scope, because **frame counts exist nowhere else**. `imageCount` counts objects; a cine clip is one object at any length. `NumberOfFrames` (0028,0008) is in the file header, so no pre-download filter on frames is possible, and anyone asking for one should be told plainly rather than pointed at `imageCount`.

`dcmread(..., stop_before_pixels=True)` is what makes inspecting cheap — never drop it. Members are read whole into `BytesIO` because a header tag can sit anywhere before the pixel data; don't "optimise" this into a fixed-size prefix read.

**The threshold applies to ultrasound only** (`min_frames_modalities`, default `US`). One CT object is one slice, so judging a CT on frames marks every slice as failing and `prune` then deletes the series entirely — that was a live footgun, not a hypothetical. A modality outside the list passes untouched, and so does an instance with no modality: never discard what cannot be classified. `instance_passes` is the single place this is decided, and both `inspect` and `prune` go through it.

`inspect` never filters: `min_frames` only fills the `passes` column and the summary, since the point is to see the distribution before choosing a cut-off. `prune` writes a **new** tree and refuses an `output_dir` inside its input; keep it non-destructive. Both log counts only — the archives with `n_passing = 0` are named in the CSV, never in a log line.

**Identifiers come from the path, never the DICOM header.** `_instance_row` used to read `PatientID` and `AccessionNumber`; those hold the *real* values whenever a download ran with no anonymization profile, so the CSV was being written with real MRNs. Both reads were deleted rather than guarded — don't reintroduce either, or a header fallback for them. `anon_mrn` / `anon_accession_number` come from `parse_anon_ids`, which lives in `crosswalk.py` beside the code that writes those paths so the two cannot drift. It requires a `P<digits>` component before filling *either* column: some sites issue real accession numbers shaped like `A0001`, and without that guard a flat tree would put a real one into a column named `anon_accession_number`. `summarise_by_exam` re-derives from the path rather than carrying a value up from the instance rows, so there is one rule, not two.

`series_uid` and `sop_instance_uid` were dropped too — no patient information, but direct keys back into the PACS. The cost is real and documented: series grouping is now `series_description` + `modality`, which merges two series sharing a description.

The CSV reports `member_index`, not `member`, and that is not cosmetic: an AIR download names members `<studyUid>/<seriesUid>/<sopUid>.dcm`, so writing the name puts both dropped UIDs straight back into a column. `iter_instances` still yields the name (`prune` needs it) but only the index reaches the CSV. The index counts *every* member including skipped ones, so it stays a valid index into `ZipFile.namelist()` — don't "fix" it to count only DICOM instances.

### Pseudonymisation (`crosswalk.py`)

Three assign-on-first-seen maps, all persisted: `mrn` → `P0001`, `(mrn, accession)` → `A0001`, `(mrn, us_accession, ct_accession)` → `visit-01`. Reloading before assigning anything is what makes a resumed or extended run reuse identifiers instead of filing a patient twice, so **the crosswalk is part of the dataset, not a log**.

- Exams key on the **pair**, never the accession alone (an accession number repeats across patients). The counter is global, so an `A` id belongs to exactly one patient.
- Counters resume from `max(numeric suffix) + 1`, never from a row count. A truncated or hand-edited file would otherwise reissue an id a folder on disk already uses, silently merging two patients.
- Rows are appended **immediately** and `record()` is called **before** the download. A row for an exam that then failed costs nothing; an archive with no way back cannot be repaired.
- One row per `archive_path`, not per `(mrn, accession)`: a CT matched to two ultrasounds is copied into both visits, so joins on the anon pair are 1:many by design.
- The first five columns carry no PHI and the last three do. That ordering is load-bearing — `cut -d, -f1-5` is the shareable projection.
- An MRN differing from a known one only by leading zeros is filed as a separate patient (it legally is one) but **warns**, because far more often it is a zero lost to Excel upstream. `int()` appears only on the digits of generated ids, which are counters this module minted, never identifiers.

### Cohort download layout (`us_ct/cohort.py`)

Nothing in an output path is an identifier: `<output>/P0001/visit-01/{us,ct}/A0001.zip`. The visit folder is an **ordinal within the patient**, not a date — it exists to order a patient's FAST-CT pairs, which was the date's only job. Rows are sorted by `us_date_time` per patient before numbering, and sorted **before** `--n` slices them, so a verification run and the full run assign identical ids. Ordinals are never renumbered (that would move folders already on disk); a late addition that predates a numbered visit appends and warns. Because each pair gets its own ordinal, two pairs on one day cannot collide — the old `claimed` index-suffixing is gone and should not come back.

The crosswalk defaults to `<output>_crosswalk.csv` beside the cohort and a path **inside** `output` is refused, mirroring `prune`'s check: a copy of the tree would otherwise carry the key with the lock. `--dry_run` builds it `read_only=True`, so a preview over a started cohort is exact and writes nothing.

`ct_date_time` is read via `.get(..., "")` and deliberately **not** in `REQUIRED_COLUMNS`, following the `us_image_count` precedent, so an older matched CSV still works. The in-memory dedupe cache for a repeated CT keys on the anon pair, so nothing outliving a single call holds a real identifier. `_safe_component` now only ever sees generated components; it stays as defence in depth, and its tests with it.

Scope is deliberate: the plain `air_download` path (`build_exam_output_path`) still names files `<accession>.zip`. It is ad-hoc use where the caller supplied the accession, it has no cohort identity for a crosswalk to attach to, and it is on the path of *every* download including this one. Don't "finish the job" by pseudonymising it.

`--thinnest-axial` is not a flag here — CT always gets the thinnest axial series alone, US always gets every series. `--skip_existing` (default on) is what makes `--n 1` → inspect → full run cheap, and it is also the resume path; `_download_single_exam` opens with `"wb"`, so without it a re-run re-fetches everything. A failed exam is counted and the loop continues. Exception detail is logged at DEBUG only, since it can carry an identifier.

Several ultrasounds can precede one CT, so a CT accession can repeat across rows. `n_preceding_us` / `us_rank_before_ct` / `is_closest_us` make that explicit, and a run warns via `count_ambiguous_cts`. The count is taken over **all** of the patient's ultrasounds, not just the paired ones, so a CT stays flagged when a preceding ultrasound was paired elsewhere — it describes the clinical picture, not the pairing strategy. Rank is resolved by identity (`u is us`), not equality, because two ultrasounds can share a timestamp and compare equal as dicts.

### Conversion to ML-loadable arrays (`us_ct/convert.py`)

The two halves need opposite operations. A CT arrives as a few hundred
single-frame objects that are one volume, so conversion *assembles*. An
ultrasound arrives as cine clips of different views, so conversion *separates*.

- **Slices are ordered by `ImagePositionPatient` projected onto the slice
  normal, never by `InstanceNumber`.** Scanners assign instance numbers wrongly
  often enough to produce a silently shuffled volume; a test pins this with a
  series whose instance numbers contradict its positions.
- **NIfTI, not Zarr, for CT** — not a performance judgement. The open CT organ
  segmentors (TotalSegmentator and relatives) are nnU-Net models that read and
  write NIfTI, so any other container means converting out and back.
- **NIfTI, not a bare array** — `PixelSpacing` varies per patient while slice
  thickness does not, so voxels are anisotropic *and* inconsistent across the
  cohort. Resampling to a common spacing needs real millimetre geometry, and
  only the affine carries it. `build_affine` converts DICOM LPS to NIfTI RAS by
  flipping the first two world axes; `nib.aff2axcodes` on an axial series must
  give `('L', 'P', 'S')`.
- **int16 Hounsfield units.** HU are integers over roughly -1024 to 3071;
  float32 would double every volume to store nothing.
- A series whose slices disagree on size or orientation, or that holds two
  slices at one position, **raises** rather than writing a volume that is
  quietly wrong. Uneven spacing warns and proceeds on the median.
- Real cohort data is `JPEG Lossless, Process 14`, which pydicom cannot decode
  unaided — hence `pylibjpeg[libjpeg]`, added from PyPI because neither it nor
  gdcm has a conda-forge build for this Python. A missing decoder raises out of
  the run rather than marking every archive failed, since it is an environment
  problem, not a data one.

### Configuration resolution

URL: `--url` flag → `AIR_URL` in credential file → `AIR_URL` env var. Credentials: credential file → env vars. Project and profile: `-pj`/`-pf` → `AIR_PROJECT`/`AIR_PROFILE` in credential file → same env vars → `-1`, both via the shared `_resolve_id`. The credential file is dotenv-format, read via `dotenv_values`. `_resolve_url` appends a trailing slash because every endpoint is joined with `urljoin`, which drops the last path segment without one.

`-pj`, `-pf`, and `download(project=..., profile=...)` default to `None`, not `-1` — that is what distinguishes "not supplied, go look at the config" from "explicitly no project/profile". Don't reintroduce `-1` as the default or the fallback stops working. Both resolve at the top of `download()` and only when actually downloading, so a bad value fails before a long chunked search rather than after it, and `--search-only` (which needs neither) is unaffected.

### Retries

`_post` is the single choke point for every API call, so the retry loop lives there and nothing else needs to know about it. It retries connection errors, timeouts, and `RETRY_STATUS_CODES` (408/425/429/5xx) with exponential backoff, preferring a numeric `Retry-After` header. Other 4xx are returned or raised immediately.

The loop returns or raises on its final pass, which keeps two existing behaviors intact: `raise_for_status=False` callers (`download/start`) still get the error body back after retries are exhausted, and a persistent 5xx still surfaces as `HTTPError` rather than something new. Tests patch `air_download.client.time.sleep`, so keep calling it through the module rather than importing `sleep` directly.

## PHI handling — STRICT

**Never read the contents of any file that may contain PHI.** This is a hard rule, not a default to weigh against convenience.

- **Never** `Read`, `cat`, `head`, `tail`, `grep`, or otherwise open `accessions.csv`, any `*.csv` of search results, any `*crosswalk*.csv`, downloaded `*.zip`/DICOM, or the credential file. Not to check a format, not to debug a parser, not "just the first line", not even when asked to.
- The **crosswalk is the most sensitive file this package writes** — MRNs, accession numbers, and real timestamps in one place, and the only thing that can re-identify a cohort. Treat it as strictly more dangerous than anything it replaced. A cohort *tree* is now safe to name in a log or a ticket; its crosswalk never is.
- Metadata only, when you genuinely need it: `wc -l`, `ls -l`, `test -f`, and column *names* via a header-only check you have written yourself. Never row values.
- To exercise CSV-reading code, **generate synthetic data** in the scratchpad (`A1,111,...`) and read that. Never a real file from the working tree.
- `git add -A` has already swept a 25k-row `accessions.csv` into a commit once. Stage explicit paths, or check `git status` before staging, and never assume the ignore rules cover a new output directory.
- If PHI does reach git: check `origin/main` before anything else, remove from history, then `git reflog expire --expire=now --all && git gc --prune=now` to drop the blob. Report exactly what was and was not pushed.

`.gitignore` covers `accessions.csv`, `*.csv`, `*crosswalk*.csv`, `output*/`, and `*.zip`. Keep it that way — the crosswalk line is redundant with `*.csv` on purpose, so relaxing that rule cannot quietly expose it.

## Identifiers are text, never numbers

MRNs and accession numbers look numeric but are not: a leading zero is part of the identifier, and dropping it names a **different patient**. Everything here reads and writes them as strings — `csv` does that by default, and `as_identifier` (in `utils.py`) coerces anything the API might return typed as a number. Tests in `test_utils.py`, `test_match.py`, `test_cohort.py`, and `test_crosswalk.py` pin `00123456` end to end; keep them. It no longer reaches a folder name — nothing real does — so what those tests now pin is that it arrives intact in the crosswalk, which is the one file that still holds it.

Never introduce `int()`, `pandas.read_csv` without `dtype=str`, or an argparse `type=int` on any identifier path. When a user reports lost leading zeros, check outside this package first — `pandas.read_csv` and Excel both coerce on read, and Excel also on save — but verify rather than assume, since the failure silently selects the wrong patient.

Arguments and outputs are patient identifiers (MRN, accession numbers) and DICOM images. Never write real accession numbers, MRNs, or credential-file contents into the repo, test fixtures, logs, or commit messages. Tests use synthetic values and `tmp_path`.

## Known drift

`setup.py` is stale (version `0.2.0`, entry point pointing at the pre-refactor `air_download.air_download:cli`). `pyproject.toml` is the authoritative build config; prefer updating it, and don't rely on `setup.py` metadata.

## Conventions

- **CLI**: use `fire.Fire()` for **all** entry points (strict). No argparse/click. Keep
  the usage `Examples` block in the module docstring.
- **Docstrings**: NumPy-style for public functions; only `Parameters`/`Returns` required. One-line docstrings for trivial private helpers.
- **Module docstrings**: first line = bare filename, blank line, then `Description: <summary>` written directly to the reader in active voice. Keep CLI usage `Examples`.
- **Imports**: three headers — `# Standard libraries`, `# Non-standard libraries`, `# Custom libraries` (project-local `src.*` **and** relative imports together under the last), each sorted by ruff `I`; header on the group's first import. `no-lines-before` keeps relative imports under `# Custom libraries`.
- **Commits**: logical, self-contained, tree buildable at every step (dependencies first). **No** `Co-Authored-By: Claude` or any Claude attribution.

### Environments (pixi)
- `default`: All necessary requirements
