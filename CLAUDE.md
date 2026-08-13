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

Tasks mirror the workflows in README.md — `download`, `search`, `list-projects`, `list-profiles`, `match`, `probe`, `download-cohort`, `test`. They are thin wrappers over `air_download` with a flag pre-applied, and pixi appends trailing arguments, so keep them in sync when CLI flags change.

The package is installed editable into the environment, so source edits need no reinstall. `pixi.lock` is committed; `.pixi/` is not.

There is no linter or formatter configured. Existing code in `air_download/` uses Google-style docstrings and full type hints, which predates the conventions below — follow **Conventions** for new code.

## Architecture

Request flow for every operation is: `cli.py` parses args → constructs `AIRClient` → calls `client.download(...)` (which internally calls `client.search(...)`) → `filters.py` narrows results → `utils.py` builds output paths / writes CSV.

- **`client.py`** — `AIRClient`, the whole API surface. Holds a `requests.Session`, a lazily-acquired JWT (`_auth_header` property authenticates on first use, so callers never call `authenticate()` explicitly), and the project list cached from the login response.
- **`filters.py`** — `apply_inclusion_filter` / `apply_exclusion_filter`. Both take a comma-separated pattern string and do case-insensitive substring matching with OR logic over one dict field. They are applied to *exams* (on `modality` / `description`) inside `search()`, and to *series* (on `description`) inside `_download_single_exam`. Inclusion runs before exclusion. Also holds the `--thinnest-axial` series selection (see below).
- **`utils.py`** — `build_exam_output_path` (directory vs. `.zip` path disambiguation, index suffix to avoid overwriting) and `write_exams_csv` (appends to `<output>/accessions.csv`, header written only when the file is new).
- **`cli.py`** — arg parsing, logging setup, table printing. `_configure_logging` deliberately only touches the `air_download` logger so `urllib3`/`requests` stay quiet.
- **`match.py`** — cohort building, not API access: pairs two search-result CSVs into ultrasound→CT matches. Its own `air_match` entry point, and the one module written to the **Conventions** below (`fire.Fire()`, NumPy docstrings, grouped imports), since it was added after they were set. New modules should follow it rather than the older files.
- **`cohort.py`** — downloads the paired CSV `match.py` writes, into `<output>/<mrn>/<MM-DD-YY>/{us,ct}/`. Own `air_cohort` entry point. Pure orchestration over `AIRClient.download()`: it passes an explicit `.zip` path per exam, which works only because `build_exam_output_path` returns a non-existent `.zip` path verbatim — and only that function's *directory* branch calls `mkdir`, so `cohort.py` creates the parents itself. Follows the **Conventions** below, like `match.py`.
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

- Structured reports are selected on `modality == "SR"`, which is exact.
- Axial detection and thickness are heuristics over `description`, and are therefore configurable and loudly logged. Don't present them as reliable.

Details worth preserving: axial patterns match as **whole words** (`\b…\b`) specifically so `THORAX` and `TRAUMA` don't register as axial — plain substring matching, as used elsewhere in this module, would break that. `parse_slice_thickness` bounds values to 0.1–20mm so a field of view like `FOV 512MM` isn't mistaken for a thickness. When no axial series states a thickness, selection falls back to max `imageCount` and says so at INFO, because that proxy is wrong when candidate series cover different anatomy. Candidates are series whose modality is `CT` or absent; the absent case exists so a data source that omits the field doesn't select nothing.

Selection runs *after* `-s`/`-s-exclude` in `_download_single_exam`, and returns SR-only (with a warning) rather than everything when no axial matches — silently downloading a whole study would be worse than downloading too little.

### Bulk input (`--accessions-csv`)

`read_accession_pairs` (in `utils.py`) reads **(mrn, accession_number) pairs, not accessions**. That pairing is a correctness requirement, not convenience: the same accession number can belong to different patients, so querying on the accession alone can return the wrong patient's exam. Never "simplify" this to a list of accessions.

Rows missing either field are skipped with a warning rather than queried on a partial key; exact duplicate pairs are collapsed, which matters because `write_exams_csv` appends and re-running a search doubles every row. `cli.py` splits into `_run_from_csv` (drives its own progress bar, merges results and writes the CSV once under `--search-only`) and `_run_single_query`. The per-exam bar in `download()` is disabled for single-exam results so a 25k-row CSV doesn't leave 25k bars behind.

### Cohort matching (`match.py`)

Three rules define a qualifying pair, and each has a test pinning it: same patient **by MRN** (never by accession, which repeats across patients), CT **strictly after** the ultrasound (equal timestamps do not qualify — neither followed the other), and within `max_hours` **inclusive** at the boundary. Comparisons use timezone-aware datetimes so mixed offsets and midnight crossings are correct; don't reduce them to naive dates.

Default keeps the earliest qualifying CT per ultrasound; `--all_pairs` emits all. Output is overwritten rather than appended, deliberately unlike `write_exams_csv`. Log counts only — never MRNs or accession numbers.

`us_image_count` / `ct_image_count` are carried through from the `image_count` column of the search CSV via `.get(..., "")`, so a CSV predating that column still matches — don't promote it into `_REQUIRED_COLUMNS`.

`select_one_us_per_ct` (`--us_selection`) is a pure post-filter over emitted rows, deliberately *not* folded into `match_exams`, which stays untouched along with the tests pinning it. `closest` takes the highest `us_rank_before_ct` within each `(mrn, ct_accession_number)` group rather than filtering `is_closest_us == True`: that flag is computed over every ultrasound the patient has, so filtering on it couples the result to an invariant this function doesn't control, while taking the max rank guarantees exactly one survivor per CT unconditionally. `most_images` exists because the wanted ultrasound is often *not* the latest one — a full FAST holds far more objects than a single-view IVC scan. A group where no row states a count falls back to `closest` and is counted for a warning.

### Series inspection (`probe.py`)

Lists what an exam contains without downloading it. `AIRClient.list_series` is the extracted `secure/search/series` call — a plain `Query`-tagged endpoint that sits **outside** the download handshake, so it queues no retrieval and needs no project or profile. `_download_single_exam` calls the same method; keep them sharing it.

**`imageCount` counts DICOM objects, not frames.** A multi-frame ultrasound cine clip counts once however long it runs. The API exposes no `NumberOfFrames`, no slice thickness, no plane, no body part, and no protocol name — the whole per-series surface is `description`, `imageCount`, `modality`, `seriesNumber`, `seriesUid`. Don't describe a count as a frame count, and don't propose a query for anything else in that list; it does not exist.

`read_exam_pairs` dispatches on the CSV header. `matched_modalities` reads the pairing's modalities off it via `^(.+)_accession_number$`, so **nothing about US/CT is hard-coded** — any pairing works, and `--modalities` selects among what the file declares. An unprefixed `accession_number` deliberately fails that regex, which is exactly what distinguishes a search-result CSV; that branch delegates to `utils.read_accession_pairs`. Don't reintroduce a fixed column list.

It cannot reuse `cohort.read_matched_pairs`, whose `REQUIRED_COLUMNS` fixes the us/ct shape; the matched branch parses rows itself but keeps the same rules — MRN and accession required together, blanks skipped and counted, duplicate pairs collapsed (which matters because `--search-only` appends). Output is overwritten, like `match.py` and unlike `write_exams_csv`.

### Cohort download layout (`cohort.py`)

The visit folder is the **ultrasound's** date (`MM-DD-YY`), not the CT's, so a CT that crossed midnight still files under the FAST it followed. That assumes one FAST-CT visit per patient per day; a second pair claiming the same folder gets an index suffix and a warning rather than merging two visits into one.

`--thinnest-axial` is not a flag here — CT always gets SR + thinnest axial, US always gets every series. `--skip_existing` (default on) is what makes `--n 1` → inspect → full run cheap, and it is also the resume path; `_download_single_exam` opens with `"wb"`, so without it a re-run re-fetches everything. A failed exam is counted and the loop continues. Exception detail is logged at DEBUG only, since it can carry an identifier.

Several ultrasounds can precede one CT, so a CT accession can repeat across rows. `n_preceding_us` / `us_rank_before_ct` / `is_closest_us` make that explicit, and a run warns via `count_ambiguous_cts`. The count is taken over **all** of the patient's ultrasounds, not just the paired ones, so a CT stays flagged when a preceding ultrasound was paired elsewhere — it describes the clinical picture, not the pairing strategy. Rank is resolved by identity (`u is us`), not equality, because two ultrasounds can share a timestamp and compare equal as dicts.

### Configuration resolution

URL: `--url` flag → `AIR_URL` in credential file → `AIR_URL` env var. Credentials: credential file → env vars. Project and profile: `-pj`/`-pf` → `AIR_PROJECT`/`AIR_PROFILE` in credential file → same env vars → `-1`, both via the shared `_resolve_id`. The credential file is dotenv-format, read via `dotenv_values`. `_resolve_url` appends a trailing slash because every endpoint is joined with `urljoin`, which drops the last path segment without one.

`-pj`, `-pf`, and `download(project=..., profile=...)` default to `None`, not `-1` — that is what distinguishes "not supplied, go look at the config" from "explicitly no project/profile". Don't reintroduce `-1` as the default or the fallback stops working. Both resolve at the top of `download()` and only when actually downloading, so a bad value fails before a long chunked search rather than after it, and `--search-only` (which needs neither) is unaffected.

### Retries

`_post` is the single choke point for every API call, so the retry loop lives there and nothing else needs to know about it. It retries connection errors, timeouts, and `RETRY_STATUS_CODES` (408/425/429/5xx) with exponential backoff, preferring a numeric `Retry-After` header. Other 4xx are returned or raised immediately.

The loop returns or raises on its final pass, which keeps two existing behaviors intact: `raise_for_status=False` callers (`download/start`) still get the error body back after retries are exhausted, and a persistent 5xx still surfaces as `HTTPError` rather than something new. Tests patch `air_download.client.time.sleep`, so keep calling it through the module rather than importing `sleep` directly.

## PHI handling — STRICT

**Never read the contents of any file that may contain PHI.** This is a hard rule, not a default to weigh against convenience.

- **Never** `Read`, `cat`, `head`, `tail`, `grep`, or otherwise open `accessions.csv`, any `*.csv` of search results, downloaded `*.zip`/DICOM, or the credential file. Not to check a format, not to debug a parser, not "just the first line", not even when asked to.
- Metadata only, when you genuinely need it: `wc -l`, `ls -l`, `test -f`, and column *names* via a header-only check you have written yourself. Never row values.
- To exercise CSV-reading code, **generate synthetic data** in the scratchpad (`A1,111,...`) and read that. Never a real file from the working tree.
- `git add -A` has already swept a 25k-row `accessions.csv` into a commit once. Stage explicit paths, or check `git status` before staging, and never assume the ignore rules cover a new output directory.
- If PHI does reach git: check `origin/main` before anything else, remove from history, then `git reflog expire --expire=now --all && git gc --prune=now` to drop the blob. Report exactly what was and was not pushed.

`.gitignore` covers `accessions.csv`, `*.csv`, `output*/`, and `*.zip`. Keep it that way.

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
