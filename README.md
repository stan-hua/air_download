# Automated Image Retrieval (AIR) Download

A command-line and Python interface to the AIR web API. Download radiology studies (DICOM) in batch if you have this service available on your PACS system.

## Installation

### Python package (recommended)

Install directly from the git repository:

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

### Core workflows

**Download a single exam by accession number** (most common):

```bash
air_download 11111111 -c ~/air_login.txt -o output/ -pj 5 -pf 3
```

**Download all exams for a patient (MRN):**

```bash
air_download --mrn 12345 -c ~/air_login.txt -o output/ -pj 5 -pf 3
```

**Search/list available exams for a patient or accession (no download):**

```bash
air_download --mrn 12345 -c ~/air_login.txt --search-only       # prints table to stdout
air_download 11111111  -c ~/air_login.txt --search-only         # prints table to stdout
```

Add `-o output/` to also save results to `output/accessions.csv`:

```bash
air_download --mrn 12345 -c ~/air_login.txt --search-only -o output/
air_download 11111111  -c ~/air_login.txt --search-only -o output/
```

**Filter by modality, description, or series:**

```bash
air_download --mrn 12345 -c ~/air_login.txt -o output/ \
    -xm MR \
    -xd "BRAIN WITH AND WITHOUT CONTRAST" \
    -s "t1,spgr,bravo,mpr"
```

**Exclude exams or series by pattern:**

```bash
# Exclude scout and localizer exams
air_download --mrn 12345 -c ~/air_login.txt -o output/ -xm-exclude "scout,localizer"

# Exclude secondary exams
air_download --mrn 12345 -c ~/air_login.txt -o output/ -xd-exclude "secondary"

# Exclude scout series
air_download 11111111 -c ~/air_login.txt -o output/ -pj 5 -pf 3 -s-exclude "scout"

# Combine inclusion and exclusion (keep MR but exclude localizer)
air_download --mrn 12345 -c ~/air_login.txt -o output/ -xm "MR" -xm-exclude "localizer"
```

**List available projects or anonymization profiles:**

```bash
air_download -c ~/air_login.txt -lpj        # list projects
air_download -c ~/air_login.txt -lpf        # list profiles
air_download -c ~/air_login.txt -lpj -lpf   # both
```

### Full CLI reference

```
$ air_download -h
usage: air_download [-h] [--url URL] [-c CRED_PATH] [-o OUTPUT] [-pf PROFILE]
                    [-pj PROJECT] [-lpj] [-lpf] [-mrn MRN]
                    [-xm EXAM_MODALITY_INCLUSION]
                    [-xd EXAM_DESCRIPTION_INCLUSION]
                    [-xm-exclude EXAM_MODALITY_EXCLUSION]
                    [-xd-exclude EXAM_DESCRIPTION_EXCLUSION]
                    [-s SERIES_INCLUSION]
                    [-s-exclude SERIES_EXCLUSION]
                    [--search-only] [-v] [-q]
                    [ACCESSION]

Command line interface to the Automated Image Retrieval (AIR) Portal.

positional arguments:
  ACCESSION             Accession number to search or download. (default: None)

options:
  -h, --help            show this help message and exit
  --url URL             AIR API URL (e.g. https://air.<domain>.edu/api/). If
                        not provided, resolved from AIR_URL in the credential
                        file or the AIR_URL environment variable. (default: None)
  -c CRED_PATH, --cred-path CRED_PATH
                        Login credentials file (dotenv format with AIR_USERNAME,
                        AIR_PASSWORD, and optionally AIR_URL). If not provided,
                        credentials are read from environment variables.
                        (default: None)
  -o OUTPUT, --output OUTPUT
                        Output path or directory. (default: None)
  -pf PROFILE, --profile PROFILE
                        Anonymization profile ID. (default: -1)
  -pj PROJECT, --project PROJECT
                        Project ID. (default: -1)
  -lpj, --list-projects
                        List available project IDs. (default: False)
  -lpf, --list-profiles
                        List available anonymization profiles. (default: False)
  -mrn MRN, --mrn MRN   Patient MRN (Medical Record Number) to search/download
                        exams for. (default: None)
  -xm EXAM_MODALITY_INCLUSION, --exam_modality_inclusion EXAM_MODALITY_INCLUSION
                        Comma-separated list of exam modality inclusion patterns
                        (case-insensitive, OR logic). Example: 'MR,CT'
                        (default: None)
  -xd EXAM_DESCRIPTION_INCLUSION, --exam_description_inclusion EXAM_DESCRIPTION_INCLUSION
                        Comma-separated list of exam description inclusion
                        patterns (case-insensitive, OR logic). Example: 'BRAIN
                        WITH AND WITHOUT CONTRAST' (default: None)
  -xm-exclude EXAM_MODALITY_EXCLUSION, --exam_modality_exclusion EXAM_MODALITY_EXCLUSION
                        Comma-separated list of exam modality exclusion patterns
                        (case-insensitive, OR logic). Excludes matching exams.
                        (default: None)
  -xd-exclude EXAM_DESCRIPTION_EXCLUSION, --exam_description_exclusion EXAM_DESCRIPTION_EXCLUSION
                        Comma-separated list of exam description exclusion
                        patterns (case-insensitive, OR logic). Excludes matching
                        exams. (default: None)
  -s SERIES_INCLUSION, --series_inclusion SERIES_INCLUSION
                        Comma-separated list of series inclusion patterns
                        (case-insensitive, OR logic). Example for T1 type
                        series: 't1,spgr,bravo,mpr' (default: None)
  -s-exclude SERIES_EXCLUSION, --series_exclusion SERIES_EXCLUSION
                        Comma-separated list of series exclusion patterns
                        (case-insensitive, OR logic). Excludes matching series.
                        (default: None)
  --search-only         Only search for exams matching the provided parameters
                        without downloading. Works with both ACCESSION and
                        --mrn. Prints a summary table to stdout. If -o is
                        also provided, writes results to
                        <output>/accessions.csv. (default: False)
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
