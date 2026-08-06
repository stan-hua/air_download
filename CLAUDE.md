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

Tasks mirror the workflows in README.md — `download`, `search`, `list-projects`, `list-profiles`, `test`. They are thin wrappers over `air_download` with a flag pre-applied, and pixi appends trailing arguments, so keep them in sync when CLI flags change.

The package is installed editable into the environment, so source edits need no reinstall. `pixi.lock` is committed; `.pixi/` is not.

There is no linter or formatter configured. Existing code in `air_download/` uses Google-style docstrings and full type hints, which predates the conventions below — follow **Conventions** for new code.

## Architecture

Request flow for every operation is: `cli.py` parses args → constructs `AIRClient` → calls `client.download(...)` (which internally calls `client.search(...)`) → `filters.py` narrows results → `utils.py` builds output paths / writes CSV.

- **`client.py`** — `AIRClient`, the whole API surface. Holds a `requests.Session`, a lazily-acquired JWT (`_auth_header` property authenticates on first use, so callers never call `authenticate()` explicitly), and the project list cached from the login response.
- **`filters.py`** — `apply_inclusion_filter` / `apply_exclusion_filter`. Both take a comma-separated pattern string and do case-insensitive substring matching with OR logic over one dict field. They are applied to *exams* (on `modality` / `description`) inside `search()`, and to *series* (on `description`) inside `_download_single_exam`. Inclusion runs before exclusion.
- **`utils.py`** — `build_exam_output_path` (directory vs. `.zip` path disambiguation, index suffix to avoid overwriting) and `write_exams_csv` (appends to `<output>/accessions.csv`, header written only when the file is new).
- **`cli.py`** — arg parsing, logging setup, table printing. `_configure_logging` deliberately only touches the `air_download` logger so `urllib3`/`requests` stay quiet.
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

The spec marks `name`, `mrn`, `accNum`, `studyUid`, `studyDescription`, `modality`, `sourceId`, and `dateRange` as required in the query payload — send all of them, empty-string the unused ones. The response's `truncated` flag matters for broad cross-patient searches and is surfaced as a warning; `dateRange` is still hardcoded empty and is the natural next lever for bounding those searches.

### Configuration resolution

URL: `--url` flag → `AIR_URL` in credential file → `AIR_URL` env var. Credentials: credential file → env vars. The credential file is dotenv-format, read via `dotenv_values`. `_resolve_url` appends a trailing slash because every endpoint is joined with `urljoin`, which drops the last path segment without one.

## PHI handling

Arguments and outputs are patient identifiers (MRN, accession numbers) and DICOM images. Never write real accession numbers, MRNs, or credential-file contents into the repo, test fixtures, or logs. Tests use synthetic values and `tmp_path`.

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
