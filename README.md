# Automated Image Retrieval (AIR) Download

A command-line and Python interface to the AIR web API. Download radiology studies (DICOM) in batch if you have this service available on your PACS system.

## Installation

### With pixi (recommended)

[pixi](https://pixi.sh) is the supported way to work from a clone of this repository. Clone it, then let pixi build the environment from the committed lock file:

```bash
git clone https://github.com/rauschecker-sugrue-labs/air_download
cd air_download
pixi install
```

That is the only setup step — the package is installed in editable mode, and every command below runs as `pixi run <task>` without activating anything.

### Python package

To install the tool standalone (outside a clone of this repository):

```bash
pip install git+https://github.com/rauschecker-sugrue-labs/air_download
```

(modify URL if the repository lives somewhere other than GitHub)

### With container

If on Mac, use the Dockerfile to build and run in a container.
If on Linux, use the Singularity/Apptainer image.

For Singularity, build the image (once):

```bash
singularity build air_download.sif Singularity.def
```

Then use the helper script [`run_air_download.py`](run_air_download.py) to run the container with the appropriate arguments. Set the top 3 arguments (`AIR_API_URL`, `DEFAULT_PROJECT_ID`, `DEFAULT_ANONYMIZATION_PROFILE`) once in the script itself. If you don't yet have a `air_login.txt` file, it will prompt you to create one.

```bash
python run_air_download.py -h          # help message
python run_air_download.py 11111111    # download a single study
```

Or run the container directly (you will need to mount the appropriate directories):

```bash
singularity run air_download.sif -h
```

## Credentials and URL configuration

Login credentials and the API URL are stored in a dotenv-style plain text file (e.g. `~/air_login.txt`):

```bash
AIR_USERNAME=username
AIR_PASSWORD=password
AIR_URL=https://air.<domain>.edu/api/
```

Please ensure this file is reasonably secure:

```bash
chmod 600 air_login.txt
```

The **URL** is resolved from (in priority order):

1. `--url` CLI flag
2. `AIR_URL` in the credential file
3. `AIR_URL` environment variable

**Credentials** are resolved from:

1. Credential file (`AIR_USERNAME` / `AIR_PASSWORD`)
2. Environment variables (`AIR_USERNAME` / `AIR_PASSWORD`)

Setting credentials as environment variables (alternative to the file):

```bash
export AIR_USERNAME=username
export AIR_PASSWORD=password
export AIR_URL=https://air.<domain>.edu/api/
```

## Usage

Every workflow is available as a pixi task from a clone of the repository, or as the `air_download` command if you installed the package standalone. The two forms are equivalent — pixi appends whatever arguments you pass to the underlying command:

| pixi task | Runs | Purpose |
| --- | --- | --- |
| `pixi run download` | `air_download` | Download exams by accession or `--mrn` |
| `pixi run search` | `air_download --search-only` | List matching exams without downloading |
| `pixi run list-projects` | `air_download -lpj` | List available project IDs |
| `pixi run list-profiles` | `air_download -lpf` | List available anonymization profiles |
| `pixi run test` | `pytest` | Run the test suite |

### Core workflows

**Download a single exam by accession number** (most common):

```bash
pixi run download 11111111 -c ~/air_login.txt -o output/ -pj 5 -pf 3
air_download     11111111 -c ~/air_login.txt -o output/ -pj 5 -pf 3   # standalone install
```

**Download all exams for a patient (MRN):**

```bash
pixi run download --mrn 12345 -c ~/air_login.txt -o output/ -pj 5 -pf 3
```

**Search/list available exams for a patient or accession (no download):**

```bash
pixi run search --mrn 12345 -c ~/air_login.txt       # prints table to stdout
pixi run search 11111111    -c ~/air_login.txt       # prints table to stdout
```

Add `-o output/` to also save results to `output/accessions.csv`:

```bash
pixi run search --mrn 12345 -c ~/air_login.txt -o output/
pixi run search 11111111    -c ~/air_login.txt -o output/
```

**Search all patients by modality and/or study description:**

`--modality` (`-m`) and `--study-description` (`-d`) are sent to the data source as query parameters, so they find matching exams across all patients — no accession or MRN needed:

```bash
pixi run search -m CT -d "CT ABDOMEN PELVIS W CONTRAST" -c ~/air_login.txt
pixi run search -m US -d "US ED BEDSIDE" -c ~/air_login.txt
pixi run search -m CT -c ~/air_login.txt -o output/    # every CT, saved to CSV
```

`--modality` takes a single DICOM modality code (`CT`, `US`, `MR`, …; case-insensitive) and is validated before the request, so typos fail immediately rather than returning nothing. If the data source truncates a broad result set, a warning says so — narrow the query and re-run.

How the server-side query relates to the client-side filters below:

| Flag | Applied | Semantics |
| --- | --- | --- |
| `-m` / `--modality` | Server, during the query | One exact modality code |
| `-d` / `--study-description` | Server, during the query | Whatever matching the data source implements |
| `-xm`, `-xd`, `-s` (and `-exclude`) | Client, after results return | Case-insensitive substring, comma-separated, OR logic |

Because the data source decides how `-d` matches, pair it with `-xd` when you need matching you can rely on:

```bash
# Ask the server for CT, then keep only descriptions containing the phrase
pixi run search -m CT -xd "abdomen pelvis" -c ~/air_login.txt
```

Both flags work with `pixi run download` too, but a modality-only download pulls every matching exam across patients — preview it with `pixi run search` first.

**Restrict the search to a date window:**

```bash
pixi run search -m CT -ds 2024-01-15 -de 2024-01-31 -c ~/air_login.txt
pixi run search -m US -d "US ED BEDSIDE" -ds 2024-01-01 -c ~/air_login.txt   # through now
```

`--date-start` (`-ds`) and `--date-end` (`-de`) take any ISO 8601 date or datetime — `2024-01-15` or `2024-01-15T13:30:00-08:00`. A date without an offset is interpreted in your local timezone. **If `--date-end` is omitted it defaults to the current date and time.**

The data source caps how many exams a single query returns, so a window longer than 7 days is automatically searched in consecutive 7-day chunks and the results merged:

```bash
# One year → 53 queries behind the scenes, one merged result set
pixi run search -m CT -d "CT ABDOMEN PELVIS W CONTRAST" -ds 2024-01-01 -de 2025-01-01 -c ~/air_login.txt
```

Chunk boundaries touch, so an exam falling exactly on one can be returned twice; duplicates are removed (by study UID, falling back to accession number plus exam date/time). If a single chunk still comes back truncated, the warning names the window that overflowed — re-run with a smaller `--chunk-days`:

```bash
pixi run search -m CT -ds 2024-01-01 -de 2024-06-01 --chunk-days 2 -c ~/air_login.txt
```

Dates narrow a search but do not constitute one on their own — pair them with an accession, `--mrn`, `--modality`, or `--study-description`.

**Filter by modality, description, or series:**

```bash
pixi run download --mrn 12345 -c ~/air_login.txt -o output/ \
    -xm MR \
    -xd "BRAIN WITH AND WITHOUT CONTRAST" \
    -s "t1,spgr,bravo,mpr"
```

**Exclude exams or series by pattern:**

```bash
# Exclude scout and localizer exams
pixi run download --mrn 12345 -c ~/air_login.txt -o output/ -xm-exclude "scout,localizer"

# Exclude secondary exams
pixi run download --mrn 12345 -c ~/air_login.txt -o output/ -xd-exclude "secondary"

# Exclude scout series
pixi run download 11111111 -c ~/air_login.txt -o output/ -pj 5 -pf 3 -s-exclude "scout"

# Combine inclusion and exclusion (keep MR but exclude localizer)
pixi run download --mrn 12345 -c ~/air_login.txt -o output/ -xm "MR" -xm-exclude "localizer"
```

The same filter flags apply to `pixi run search` when you want to preview what a download would fetch.

**List available projects or anonymization profiles:**

```bash
pixi run list-projects -c ~/air_login.txt          # list projects
pixi run list-profiles -c ~/air_login.txt          # list profiles
pixi run download -c ~/air_login.txt -lpj -lpf     # both at once
```

### Full CLI reference

Run `pixi run download -h` (or `air_download -h`) for the current help text:

```
$ air_download -h
usage: air_download [-h] [--url URL] [-c CRED_PATH] [-o OUTPUT] [-pf PROFILE]
                    [-pj PROJECT] [-lpj] [-lpf] [-mrn MRN] [-m MODALITY]
                    [-d STUDY_DESCRIPTION] [-ds DATE_START] [-de DATE_END]
                    [--chunk-days CHUNK_DAYS] [-xm EXAM_MODALITY_INCLUSION]
                    [-xd EXAM_DESCRIPTION_INCLUSION]
                    [-xm-exclude EXAM_MODALITY_EXCLUSION]
                    [-xd-exclude EXAM_DESCRIPTION_EXCLUSION]
                    [-s SERIES_INCLUSION] [-s-exclude SERIES_EXCLUSION]
                    [--search-only] [-v] [-q]
                    [ACCESSION]

Command line interface to the Automated Image Retrieval (AIR) Portal.

positional arguments:
  ACCESSION             Accession number to search or download. (default:
                        None)

options:
  -h, --help            show this help message and exit
  --url URL             AIR API URL (e.g. https://air.<domain>.edu/api/). If
                        not provided, resolved from AIR_URL in the credential
                        file or the AIR_URL environment variable. (default:
                        None)
  -c, --cred-path CRED_PATH
                        Login credentials file (dotenv format with
                        AIR_USERNAME, AIR_PASSWORD, and optionally AIR_URL).
                        If not provided, credentials are read from environment
                        variables. (default: None)
  -o, --output OUTPUT   Output path or directory. (default: None)
  -pf, --profile PROFILE
                        Anonymization profile ID. (default: -1)
  -pj, --project PROJECT
                        Project ID. (default: -1)
  -lpj, --list-projects
                        List available project IDs. (default: False)
  -lpf, --list-profiles
                        List available anonymization profiles. (default:
                        False)
  -mrn, --mrn MRN       Patient MRN (Medical Record Number) to search/download
                        exams for. (default: None)
  -m, --modality MODALITY
                        Modality to query the server for across all patients
                        (e.g. 'CT', 'US', 'MR'). Unlike -xm, this is sent to
                        the data source as a query parameter and must be a
                        single valid modality code. (default: None)
  -d, --study-description STUDY_DESCRIPTION
                        Study description to query the server for across all
                        patients (e.g. 'CT ABDOMEN PELVIS W CONTRAST').
                        Matching is performed by the data source; use -xd for
                        guaranteed case-insensitive substring matching on the
                        returned exams. (default: None)
  -ds, --date-start DATE_START
                        Start of the date window to search, ISO 8601 (e.g.
                        '2024-01-15' or '2024-01-15T13:30:00-08:00').
                        (default: None)
  -de, --date-end DATE_END
                        End of the date window to search, ISO 8601. Defaults
                        to the current date and time when --date-start is
                        given. (default: None)
  --chunk-days CHUNK_DAYS
                        The data source caps how many exams one query returns,
                        so date windows longer than this are searched in
                        consecutive chunks and the results merged. Lower it if
                        results still come back truncated. (default: 7)
  -xm, --exam_modality_inclusion EXAM_MODALITY_INCLUSION
                        Comma-separated list of exam modality inclusion
                        patterns (case-insensitive, OR logic). Example:
                        'MR,CT' (default: None)
  -xd, --exam_description_inclusion EXAM_DESCRIPTION_INCLUSION
                        Comma-separated list of exam description inclusion
                        patterns (case-insensitive, OR logic). Example: 'BRAIN
                        WITH AND WITHOUT CONTRAST' (default: None)
  -xm-exclude, --exam_modality_exclusion EXAM_MODALITY_EXCLUSION
                        Comma-separated list of exam modality exclusion
                        patterns (case-insensitive, OR logic). Excludes
                        matching exams. (default: None)
  -xd-exclude, --exam_description_exclusion EXAM_DESCRIPTION_EXCLUSION
                        Comma-separated list of exam description exclusion
                        patterns (case-insensitive, OR logic). Excludes
                        matching exams. (default: None)
  -s, --series_inclusion SERIES_INCLUSION
                        Comma-separated list of series inclusion patterns
                        (case-insensitive, OR logic). Example for T1 type
                        series: 't1,spgr,bravo,mpr' (default: None)
  -s-exclude, --series_exclusion SERIES_EXCLUSION
                        Comma-separated list of series exclusion patterns
                        (case-insensitive, OR logic). Excludes matching
                        series. (default: None)
  --search-only         Only search for exams matching the provided parameters
                        without downloading. Works with both ACCESSION and
                        --mrn. Prints a summary table to stdout. If -o is also
                        provided, writes results to <output>/accessions.csv.
                        (default: False)
  -v, --verbose         Enable verbose (DEBUG level) logging. (default: False)
  -q, --quiet           Suppress all output except errors. (default: False)
```

## Python API

You can also use `air_download` as a library:

```python
from pathlib import Path
from air_download import AIRClient

# URL + credentials resolved from the credential file
client = AIRClient(cred_path="/path/to/air_login.txt")

# Download a single exam by accession
client.download(accession="11111111", project=5, profile=3, output=Path("output/"))

# Download all exams for a patient
client.download(mrn="12345", project=5, profile=3, output=Path("output/"))

# Search only (returns list of exam dicts, no download)
exams = client.search(mrn="12345", exam_modality_inclusion="MR")

# Search by accession (returns exam details without downloading)
exams = client.search(accession="11111111")

# Search across all patients by modality and/or study description
exams = client.search(modality="CT", study_description="CT ABDOMEN PELVIS W CONTRAST")
exams = client.search(modality="US", study_description="US ED BEDSIDE")
exams = client.search(modality="CT")   # every CT the data source returns

# Restrict to a date window; windows longer than chunk_days are searched
# in consecutive chunks and merged. date_end defaults to now.
exams = client.search(modality="CT", date_start="2024-01-01", date_end="2024-06-01")
exams = client.search(modality="CT", date_start="2024-01-01", chunk_days=2)

# Use exclusion filters
exams = client.search(
    mrn="12345",
    exam_modality_inclusion="MR",
    exam_modality_exclusion="localizer"
)

# Download with series exclusion
client.download(
    accession="11111111",
    project=5,
    profile=3,
    output=Path("output/"),
    series_inclusion="t1,t2",
    series_exclusion="scout"
)

# List projects and profiles
projects = client.list_projects()
profiles = client.list_profiles()
```

If the URL is not in the credential file, pass it explicitly:

```python
client = AIRClient(url="https://air.<domain>.edu/api/", cred_path="/path/to/air_login.txt")
```

From a clone, run library code inside the environment with `pixi run python your_script.py`, or open a shell with the environment activated using `pixi shell`.

## Development

```bash
pixi run test                                        # full test suite
pixi run pytest tests/test_filters.py -v             # a single test file
pixi add <package>                                   # add a conda-forge dependency
pixi add --pypi <package>                            # only if not on conda-forge
```

Dependencies live in `pyproject.toml` under `[tool.pixi.dependencies]`; `pixi.lock` is committed so environments are reproducible. Keep the bounds there in sync with `[project.dependencies]`, which is what a standalone `pip install` uses.
