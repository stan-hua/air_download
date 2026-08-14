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
AIR_PROJECT=5
AIR_PROFILE=3
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

The **project** (`AIR_PROJECT`) and **anonymization profile** (`AIR_PROFILE`), both integers, are each resolved from:

1. `-pj` / `--project` and `-pf` / `--profile` CLI flags
2. `AIR_PROJECT` / `AIR_PROFILE` in the credential file
3. `AIR_PROJECT` / `AIR_PROFILE` environment variables

Set them once and you can drop `-pj` and `-pf` from every command. `pixi run list-projects` and `pixi run list-profiles` show the valid IDs. A non-integer value fails immediately, naming where it came from. Neither is needed for `--search-only`.

Setting these as environment variables (alternative to the file):

```bash
export AIR_USERNAME=username
export AIR_PASSWORD=password
export AIR_URL=https://air.<domain>.edu/api/
export AIR_PROJECT=5
export AIR_PROFILE=3
```

## Usage

Every workflow is available as a pixi task from a clone of the repository, or as the `air_download` command if you installed the package standalone. The two forms are equivalent — pixi appends whatever arguments you pass to the underlying command:

| pixi task | Runs | Purpose |
| --- | --- | --- |
| `pixi run download` | `air_download` | Download exams by accession or `--mrn` |
| `pixi run search` | `air_download --search-only` | List matching exams without downloading |
| `pixi run list-projects` | `air_download -lpj` | List available project IDs |
| `pixi run list-profiles` | `air_download -lpf` | List available anonymization profiles |
| `pixi run match` | `air_match` | Match ultrasound exams to the CTs that followed |
| `pixi run download-cohort` | `air_cohort` | Download a matched cohort into per-patient visit folders |
| `pixi run frames` | `air_frames` | Count frames in downloaded DICOMs, and prune to the multi-frame clips |
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

With `AIR_PROJECT` and `AIR_PROFILE` set in the credential file you can drop `-pj` and `-pf` entirely — see [Credentials and URL configuration](#credentials-and-url-configuration):

```bash
pixi run download --mrn 12345 -c ~/air_login.txt -o output/
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

Both flags work with `pixi run download` too, but a modality-only download pulls every matching exam across patients — preview it with `pixi run search` first. The client-side filters are covered in [Narrowing to the exams and series you want](#narrowing-to-the-exams-and-series-you-want).

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

**List available projects or anonymization profiles:**

```bash
pixi run list-projects -c ~/air_login.txt          # list projects
pixi run list-profiles -c ~/air_login.txt          # list profiles
pixi run download -c ~/air_login.txt -lpj -lpf     # both at once
```

### Narrowing to the exams and series you want

The client-side filters from the table above run on whatever the server returned. They apply to `pixi run search` as well as `pixi run download`, so you can preview what a download would fetch.

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

#### Keeping only the thinnest axial CT series

```bash
pixi run download -m CT -ds 2024-01-01 -c ~/air_login.txt -o output/ --thinnest-axial
```

For each exam this keeps exactly **one** series — the axial CT reconstruction with the thinnest slices — and drops everything else: scouts, reformats, the thicker axial reconstructions, and structured reports:

```
SCOUT                 2 images   CT   dropped
AXIAL 5MM STD        60 images   CT   dropped
AXIAL 0.625MM BONE  480 images   CT   kept    <- thinnest
AXIAL 2.5MM SOFT    120 images   CT   dropped
COR 3MM MPR          90 images   CT   dropped
Dose Report           1 image    SR   dropped
```

Structured reports carry no image data, so they are dropped with the rest. If you want them, leave `--thinnest-axial` off and narrow with `-s`/`-s-exclude` instead.

<a id="what-the-api-reports-per-series"></a>
**What the API reports per series.** Only `description`, `imageCount`, `modality`, `seriesNumber`, and `seriesUid` — there is no slice thickness, no imaging plane, no body part, and no protocol name anywhere in the API. `imageCount` counts DICOM **objects, not frames**, so a multi-frame cine clip counts once however long it runs. Everything below follows from that gap, and it is why series selection has to read descriptions.

The selection is therefore a heuristic:

- **Axial** means the description contains `ax`, `axial`, `tra`, `trans`, or `transverse` as a **whole word**, case-insensitive. Whole-word matching is what keeps `THORAX` and `TRAUMA` from counting as axial. Override with `--axial-patterns "ax,axial"` if your site names things differently.
- **Thinnest** is read from the description when the protocol states it — `AXIAL 0.625MM` → 0.625mm. Values below 0.1mm or above 20mm are ignored as something other than a thickness (a field of view, for instance), and the smallest plausible value wins if several appear.
- **If no axial series states a thickness**, the one with the most images is chosen instead, on the basis that more images means thinner slices at equal coverage. This is logged when it happens, since it is wrong if the series cover different anatomy.
- **If nothing matches as axial**, a warning names the patterns tried and **nothing is kept** — no archive is written for that exam. Widen `--axial-patterns` and re-run.

Run it under `-v` the first few times to see which series each exam actually selected:

```bash
pixi run download 11111111 -c ~/air_login.txt -o output/ --thinnest-axial -v
INFO: Thinnest axial series: 'AXIAL 0.625MM BONE' (0.625mm, 480 images).
INFO: Series selection: from 6 to 1 (thinnest axial).
```

`--thinnest-axial` runs after `-s` and `-s-exclude`, so those can pre-trim the candidates.

### Building a cohort, then downloading it

Search first and write the results to a CSV, one directory per cohort:

```bash
pixi run search -m CT -d "CT ABDOMEN PELVIS W CONTRAST" -ds 2021-01-01 -de 2023-12-31 -o output-ct_abdomen_pelvis/
pixi run search -m US -d "US ED BEDSIDE" -ds 2021-01-01 -de 2023-12-31 -o output-us_ed_bedside/
```

Each writes `<output>/accessions.csv`. Three years is well past the 7-day query cap, so each of these runs as ~157 chunked queries merged into one de-duplicated result set. Review the CSV before committing to a download:

```
mrn,accession_number,date_time,sex,birthdate,description,image_count
A1,111,2021-03-02,,,"CT ABDOMEN PELVIS W CONTRAST, AXIAL",480
A2,222,2022-06-11,,,CT ABDOMEN PELVIS W CONTRAST,300
```

Note that `--search-only` **appends** to `accessions.csv` if one is already there, so use a fresh directory per cohort or delete the file between runs.

> **These CSVs contain PHI** — MRNs, accession numbers, and birthdates. `.gitignore` covers `accessions.csv`, `*.csv`, `output*/`, and `*.zip` so they cannot be committed by accident. Keep search output out of the repository, and check `git status` before staging.

> **MRNs and accession numbers are text, not numbers.** They are written and read as strings at every step here, so a leading zero survives — `00123456` stays `00123456` through the search CSV, the matched CSV, and into the cohort crosswalk. **Anything else that opens these files can still destroy them.** That matters more than it looks: an MRN that lost its zero upstream is a *different* key, so it would be filed as a second patient with its own folder. A run warns when two MRNs in one cohort differ only by leading zeros. `pandas.read_csv` infers `00123456` as the integer `123456`, and Excel does the same on open *and on save*, silently pointing you at a different patient. Read them as text:
>
> ```python
> # pandas: force the identifier columns to stay strings
> df = pd.read_csv("matched_us_ct.csv", dtype=str)
> df = pd.read_csv("accessions.csv", dtype={"mrn": str, "accession_number": str})
> ```
>
> In Excel, use Data ▸ From Text/CSV and set those columns to *Text* rather than double-clicking the file. If a CSV has already been through Excel, the zeros are gone from the file — re-export it from the original search rather than trying to pad them back, since you cannot tell how many digits were lost.

#### Downloading from the CSV

Feed the CSV straight back in with `--accessions-csv`:

```bash
# CT: keep only the thinnest axial series
pixi run download --accessions-csv output-ct_abdomen_pelvis/accessions.csv \
    -o ct_dicom/ --thinnest-axial

# US: every series in the exam
pixi run download --accessions-csv output-us_ed_bedside/accessions.csv \
    -o us_dicom/
```

**Each row is looked up by MRN *and* accession number together.** The same accession number can belong to more than one patient, so querying on the accession alone can return another patient's exam. Both columns are therefore required, and a row missing either is skipped with a warning rather than guessed at:

| CSV rows | Result |
| --- | --- |
| `A1,111` and `A2,111` | Two lookups. Same accession, different patients, both fetched |
| `A1,111` twice | One lookup. Exact duplicates are collapsed |
| `,111` or `A1,` | Skipped, counted in a warning |

Duplicate collapsing matters because `--search-only` appends: a search re-run into the same directory leaves every row twice, and the download would otherwise fetch each exam twice.

Only `mrn` and `accession_number` are read; column order is irrelevant and extra columns are ignored, so any CSV with those two headers works, not just one this tool wrote. A missing header fails immediately, naming the columns it did find.

`--accessions-csv` cannot be combined with `ACCESSION` or `--mrn`, since the file already supplies both. Search-level flags such as `-m`, `-d`, and the date window are not applied — the CSV already fixes exactly which exams are wanted — while series-level flags still apply to each exam, which is why `--thinnest-axial` is on the CT command and not the US one. Set `AIR_PROJECT` and `AIR_PROFILE` in the credential file so they do not need repeating.

Pair it with `--search-only` to re-check a cohort without downloading; results are merged and written once rather than appended per row:

```bash
pixi run download --accessions-csv cohort.csv --search-only -o verified/
```

From Python:

```python
from pathlib import Path
from air_download import AIRClient
from air_download.utils import read_accession_pairs

client = AIRClient(cred_path="~/air_login.txt")   # one login for the whole run

for mrn, accession in read_accession_pairs(Path("output-ct_abdomen_pelvis/accessions.csv")):
    client.download(
        mrn=mrn,
        accession=accession,
        output=Path("ct_dicom/"),
        thinnest_axial=True,
    )
```

### Matching ultrasounds to the CTs that followed

`pixi run match` pairs two search-result CSVs into a cohort of patients who had an ED ultrasound and then a CT:

```bash
pixi run match --us_csv output-us_ed_bedside/accessions.csv \
               --ct_csv output-ct_abdomen_pelvis/accessions.csv \
               --output matched_us_ct.csv
```

A pair qualifies when all three hold:

1. **Same patient** — matched on `mrn`, never on accession number, which repeats across patients.
2. **CT strictly after the ultrasound** — a CT before it is excluded, and so is one sharing its timestamp, since neither followed the other.
3. **Within 24 hours** — inclusive at exactly 24h; `--max_hours` changes the window.

Output is one row per matched ultrasound:

```
mrn,us_accession_number,us_date_time,us_description,us_image_count,ct_accession_number,ct_date_time,ct_description,ct_image_count,hours_between
A1,U1,2021-03-02T08:00:00-08:00,US ED BEDSIDE,22,C1,2021-03-02T14:00:00-08:00,CT ABDOMEN PELVIS W CONTRAST,480,6.0
A4,U4,2021-04-01T22:00:00-07:00,US ED BEDSIDE,18,C4,2021-04-02T03:00:00-07:00,CT ABDOMEN PELVIS W CONTRAST,512,5.0
```

`hours_between` is there so you can sanity-check the window and tighten it afterwards without re-running.

`us_image_count` and `ct_image_count` are carried straight through from the `image_count` column of the search results. They count [objects, not frames](#what-the-api-reports-per-series), but they still separate a multi-view FAST from a single-view bedside scan. An older `accessions.csv` written before that column existed still matches; the two columns come out empty.

When a patient has **several qualifying CTs**, the default keeps the **earliest** one — the CT that actually followed the ultrasound. `--all_pairs` emits every qualifying CT instead:

```bash
pixi run match --us_csv us/accessions.csv --ct_csv ct/accessions.csv \
               --max_hours 48 --all_pairs --output matched_48h.csv
```

#### When several ultrasounds precede the same CT

This happens — a repeat scan a few hours before the CT, for instance — and it means one CT legitimately pairs with more than one ultrasound. Every row carries three columns so it is never silent:

| Column | Meaning |
| --- | --- |
| `n_preceding_us` | How many ultrasounds qualify for this CT (same patient, inside the window). `1` means unambiguous |
| `us_rank_before_ct` | This ultrasound's position among them, `1` = earliest |
| `is_closest_us` | True for the ultrasound immediately before the CT |

```
mrn  us_accession  us_date_time               ct_accession  hours_between  n_preceding_us  us_rank_before_ct  is_closest_us
A1   U_EARLY       2021-03-02T06:00:00-08:00  C1            4.0            2               1                  False
A1   U_LATE        2021-03-02T09:00:00-08:00  C1            1.0            2               2                  True
A2   U_SOLO        2021-03-05T08:00:00-08:00  C2            4.0            1               1                  True
```

A run also warns when it happens at all:

```
WARNING: 1 CT exam(s) had more than one ultrasound before them inside the window,
so those CTs appear on several rows. Filter on is_closest_us to keep one
ultrasound per CT, pass --us_selection, or inspect n_preceding_us and
us_rank_before_ct to decide case by case.
```

`--us_selection` resolves it in the run itself, keeping exactly one row per CT:

| Value | Keeps |
| --- | --- |
| `all` (default) | Every candidate — nothing dropped |
| `closest` | The ultrasound nearest the CT (equivalent to filtering `is_closest_us`) |

```bash
pixi run match --us_csv us/accessions.csv --ct_csv ct/accessions.csv \
               --us_selection closest
```

**Timing is the only signal available at this stage.** Ranking on `us_image_count` was tried and removed: `image_count` counts DICOM *objects*, not frames, so a single-view study saved as twenty stills outranks a four-clip FAST. If the ultrasound you want is not the one nearest the CT, the honest answer is that the API cannot tell you which it is — settle it with real frame counts from [`pixi run frames`](#counting-frames-and-keeping-only-the-real-cine-clips) after downloading, then filter the matched CSV yourself.

Alternatively, filter afterwards on `is_closest_us`:

```bash
pixi run python -c "
import csv
rows = [r for r in csv.DictReader(open('matched_us_ct.csv')) if r['is_closest_us'] == 'True']
w = csv.DictWriter(open('matched_closest.csv','w',newline=''), fieldnames=rows[0].keys())
w.writeheader(); w.writerows(rows)
print(len(rows), 'rows kept')
"
```

`n_preceding_us` counts over **every** ultrasound in the input, not only the ones this run paired. So a CT is still flagged as ambiguous when one of its preceding ultrasounds was paired to a different CT — the count reflects the clinical picture, not the pairing strategy. Ultrasounds outside the window never count toward it.

Timestamps are compared as absolute instants, so mixed UTC offsets and pairs crossing midnight are handled correctly. Rows without an MRN or with an unreadable `date_time` are skipped and counted in a warning; identifiers are never logged. The output file is **overwritten**, not appended to, unlike `accessions.csv`.

Both inputs need `mrn`, `accession_number`, and `date_time` columns — any CSV with those works, not just one this tool wrote.

#### Downloading a matched cohort

`pixi run download-cohort` takes the matched CSV directly and lays the exams out one folder per patient, one subfolder per visit:

```bash
pixi run download-cohort --matched_csv matched_us_ct.csv \
    --output output-cohort/ --cred_path ~/air_login.txt
```

```
output-cohort/
└── P0001/                   # a patient
    └── visit-01/            # their first FAST-CT pair
        ├── us/A0001.zip
        └── ct/A0002.zip

output-cohort_crosswalk.csv  # the only way back to real identifiers
```

**Nothing in that tree is an identifier.** Patients, exams, and visits are all numbered, so the paths are safe to paste into a ticket, a traceback, or a shared terminal. The visit folder counts *within* a patient in the order their ultrasounds happened — it orders a patient's FAST-CT pairs, which is all the old date-named folder was doing, without being a date. The ultrasound keeps every series; the CT is reduced to its thinnest axial series alone, exactly as `--thinnest-axial` does — that split is built in, so there is no per-modality flag to remember. A CT with no axial series yields no archive and is counted as failed, so check the log rather than assuming a silent success.

Only `mrn`, `us_accession_number`, `us_date_time`, and `ct_accession_number` are read (plus `ct_date_time` when present), so a CSV from an older `match` run works too. Rows missing any of the four are skipped and counted in a warning.

Verify before committing to a long download. `--dry_run` prints the paths and makes no network call; `--n` downloads just the first rows for real:

```bash
pixi run download-cohort --matched_csv matched_us_ct.csv --output output-cohort/ --dry_run
pixi run download-cohort --matched_csv matched_us_ct.csv --output output-cohort/ \
    --cred_path ~/air_login.txt --n 1
```

An archive already on disk is skipped (`--skip_existing`, on by default), so the verification pair is not fetched twice when you then run the whole cohort, and an interrupted run resumes where it stopped. A failed exam is counted and the run continues rather than aborting; re-run to retry it. Only counts are logged — never an MRN or accession number.

##### The crosswalk

`<output>_crosswalk.csv` holds the mapping, one row per archive:

```
anon_mrn, anon_accession_number, visit_folder, exam_type, archive_path, mrn, accession_number, date_time
```

The first five columns carry no PHI, so `cut -d, -f1-5 output-cohort_crosswalk.csv` is a shareable description of the cohort's entire structure. The last three are the sensitive half.

- **Guard it like the data it unlocks.** It is a more dangerous file than anything it replaced: MRNs, accession numbers, and real timestamps in one place. `.gitignore` covers `*crosswalk*.csv` explicitly.
- **It must live outside the cohort directory.** A path inside `--output` is refused, because a `tar` or `rsync` of the tree would otherwise ship the key with the lock. Use `--crosswalk_csv ~/private/cohort_crosswalk.csv` to put it somewhere else entirely.
- **Losing it orphans the cohort permanently.** There is no other route back.

**Adding to a cohort later is safe.** Identifiers are assigned on first sight and reloaded from the crosswalk before anything new is handed out, so a second run over a longer matched CSV reuses every ID the first run assigned: existing patients keep their number, their archives are skipped rather than re-fetched, and only genuinely new patients, exams, and visits get new IDs. A visit added later appends as the patient's next ordinal — ordinals are never renumbered, since that would move folders already on disk. When a late addition turns out to predate a visit already numbered, the run says so; sort by the crosswalk's `date_time` when you need true order.

To join a downstream CSV back to real patients — `frames.csv` reports `anon_mrn` and `anon_accession_number` for exactly this:

```bash
pixi run python -c "
import csv
key = {(r['anon_mrn'], r['anon_accession_number']): r
       for r in csv.DictReader(open('output-cohort_crosswalk.csv'))}
for r in csv.DictReader(open('frames.csv')):
    real = key.get((r['anon_mrn'], r['anon_accession_number']))
    ...
"
```

One CT can be matched to two ultrasounds, in which case it is copied into both visits and the join is 1:many on `archive_path`.

> **What this does and does not de-identify.** The tree and the derived CSVs are pseudonymous. The DICOM files inside are whatever your AIR anonymization profile left them as — `PatientID`, `AccessionNumber`, and `StudyDate` in the headers are untouched by this package. A pseudonymous tree is not a shareable dataset; check `pixi run list-profiles` for what is actually being applied to the pixels' metadata.

To download only one side of the cohort instead, split out whichever accession column you want and use `--accessions-csv`. Note that this is the plain download path, which is **not** pseudonymized — it names each archive `<accession>.zip`, deliberately, since it is ad-hoc use where you supplied the accession yourself and there is no cohort for a crosswalk to belong to:

```bash
pixi run python -c "
import csv
rows = list(csv.DictReader(open('matched_us_ct.csv')))
with open('matched_ct_only.csv','w',newline='') as f:
    w = csv.writer(f); w.writerow(['mrn','accession_number'])
    w.writerows([[r['mrn'], r['ct_accession_number']] for r in rows])
"
pixi run download --accessions-csv matched_ct_only.csv -o matched_ct_dicom/ --thinnest-axial
```

### Counting frames, and keeping only the real cine clips

**Frame counts are the one thing the API cannot give you.** `imageCount` counts DICOM *objects*: an ultrasound cine clip is a single object whether it holds 2 frames or 200, so a FAST reporting `image_count: 7` may well contain thousands of frames. `NumberOfFrames` (0028,0008) lives in the file header and appears nowhere in the API, so **there is no way to filter on frames before downloading**. It has to be a second pass over what you already have.

`pixi run frames inspect` reads the header of every downloaded instance — stopping before the pixel data, so it is far cheaper than the download that produced them — and writes two CSVs:

```bash
pixi run frames inspect --input output-cohort/ --output frames.csv --min_frames 60
```

`frames.csv`, one row per instance — columns `archive`, `member_index`, `anon_mrn`, `anon_accession_number`, `series_description`, `modality`, `n_frames`, `rows`, `columns`, `passes`:

```
anon_mrn  anon_accession_number  member_index  series_description  n_frames  passes
P0001     A0001                             0  RUQ                      148  True
P0001     A0001                             1  LUQ                      132  True
P0001     A0001                             4  (still)                    1  False
P0002     A0003                             0  IVC                       22  False
```

`member_index` is the instance's position in the archive's `ZipFile.namelist()`, not its filename. AIR names members `<studyUid>/<seriesUid>/<sopUid>.dcm`, so the name would put the UIDs below straight back into the CSV. To pull one out: `zipfile.ZipFile(archive).namelist()[member_index]`.

`frames_exams.csv`, one row per archive, which is where you spot exams that would drop out of the cohort entirely:

```
anon_mrn  anon_accession_number  n_instances  n_passing  max_frames  total_frames
P0001     A0001                            7          4         148           450
P0002     A0003                            2          0          22            23
```

**The identifiers come from the path, never the DICOM header.** `PatientID` and `AccessionNumber` in the header hold the *real* values whenever a download ran without an anonymization profile, so reading them would write real identifiers into a CSV; nothing here reads them. An archive outside the pseudonymous cohort layout leaves both columns empty rather than guessing — some sites issue real accession numbers that look exactly like a generated `A0001` — and a run reports how many archives that covers. Join back through the [crosswalk](#the-crosswalk).

`series_uid` and `sop_instance_uid` are not written either. They name no patient, but they are direct keys back into the PACS. The cost is that series grouping is now by `series_description` and `modality`, which merges two genuinely different series that share a description.

**`--min_frames` applies to ultrasound only.** One CT object is one slice, so judging a CT on frame count rejects every slice and `prune` then deletes the whole series. `--min_frames_modalities` (default `US`) names the modalities the threshold means anything for; everything else passes untouched, and an instance with no modality passes too, since nothing that cannot be classified should be discarded. Pass an empty string to apply it to all.

`--min_frames` only fills in `passes` and the summary counts — **every instance is reported either way**, so you can look at the distribution before committing to a threshold. A run warns when any archive has `n_passing = 0`, naming the count rather than the archives; the CSV has the rows.

Once the cut-off looks right, prune into a **new** tree. The source archives are never modified:

```bash
pixi run frames prune --input output-cohort/ --output_dir output-60frames/ --min_frames 60
```

Each archive is rewritten at the same relative path holding only the qualifying instances; one left with nothing is not written at all and is counted in a warning. Writing inside `--input` is refused, so a run cannot read its own output.

### Retries

Requests that fail transiently — connection errors, timeouts, rate limiting (`429`), and server errors (`5xx`) — are retried automatically with exponential backoff: 1s, 2s, 4s, 8s, 16s, capped at 60s. If the server sends a numeric `Retry-After` header, that wins over the computed delay. Ordinary client errors such as `401` or `404` fail immediately, since retrying cannot help.

This matters most for long chunked date searches, which issue many queries in quick succession and are the likeliest thing to trip a rate limit. Each retry logs a warning naming the reason and the wait.

```bash
pixi run search -m CT -ds 2024-01-01 --max-retries 10 -c ~/air_login.txt   # more patient
pixi run search -m CT -ds 2024-01-01 --max-retries 0  -c ~/air_login.txt   # fail fast
```

From the Python API, tune the policy on the client:

```python
client = AIRClient(cred_path="...", max_retries=10, backoff_factor=2.0, max_backoff=120)
```

### Full CLI reference

Run `pixi run download -h` (or `air_download -h`) for the current help text:

```
$ air_download -h
usage: air_download [-h] [--url URL] [-c CRED_PATH] [-o OUTPUT] [-pf PROFILE]
                    [-pj PROJECT] [-lpj] [-lpf] [-mrn MRN]
                    [--accessions-csv ACCESSIONS_CSV] [-m MODALITY]
                    [-d STUDY_DESCRIPTION] [-ds DATE_START] [-de DATE_END]
                    [--chunk-days CHUNK_DAYS] [-xm EXAM_MODALITY_INCLUSION]
                    [-xd EXAM_DESCRIPTION_INCLUSION]
                    [-xm-exclude EXAM_MODALITY_EXCLUSION]
                    [-xd-exclude EXAM_DESCRIPTION_EXCLUSION]
                    [-s SERIES_INCLUSION] [-s-exclude SERIES_EXCLUSION]
                    [--thinnest-axial] [--axial-patterns AXIAL_PATTERNS]
                    [--search-only] [--max-retries MAX_RETRIES] [-v] [-q]
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
                        Anonymization profile ID. If omitted, read from
                        AIR_PROFILE in the credential file or the AIR_PROFILE
                        environment variable. (default: None)
  -pj, --project PROJECT
                        Project ID. If omitted, read from AIR_PROJECT in the
                        credential file or the AIR_PROJECT environment
                        variable. (default: None)
  -lpj, --list-projects
                        List available project IDs. (default: False)
  -lpf, --list-profiles
                        List available anonymization profiles. (default:
                        False)
  -mrn, --mrn MRN       Patient MRN (Medical Record Number) to search/download
                        exams for. (default: None)
  --accessions-csv ACCESSIONS_CSV
                        CSV of exams to download, with 'mrn' and
                        'accession_number' columns (as written by --search-
                        only). Both are used for each lookup, since the same
                        accession number can belong to more than one patient.
                        Cannot be combined with ACCESSION or --mrn. (default:
                        None)
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
  --thinnest-axial      For each exam, keep only the single axial CT series
                        with the thinnest slices, dropping everything else
                        including structured reports. Thickness is read from
                        the series description (e.g. '0.625MM'); if no axial
                        series states one, the series with the most images
                        wins. An exam with no axial series is skipped. Applied
                        after -s and -s-exclude. (default: False)
  --axial-patterns AXIAL_PATTERNS
                        Comma-separated plane names identifying an axial
                        series, matched as whole words in the description
                        (case-insensitive). (default:
                        ax,axial,tra,trans,transverse)
  --search-only         Only search for exams matching the provided parameters
                        without downloading. Works with both ACCESSION and
                        --mrn. Prints a summary table to stdout. If -o is also
                        provided, writes results to <output>/accessions.csv.
                        (default: False)
  --max-retries MAX_RETRIES
                        Number of times to retry a request after a connection
                        error, timeout, rate limit, or server error. Delays
                        double each time. Use 0 to fail on the first error.
                        (default: 5)
  -v, --verbose         Enable verbose (DEBUG level) logging. (default: False)
  -q, --quiet           Suppress all output except errors. (default: False)
```

## Python API

You can also use `air_download` as a library:

```python
from pathlib import Path
from air_download import AIRClient

# URL, credentials, and optional AIR_PROJECT / AIR_PROFILE come from the file
client = AIRClient(cred_path="/path/to/air_login.txt")

# Omit `project` and `profile` to use AIR_PROJECT / AIR_PROFILE
client.download(accession="11111111", output=Path("output/"))

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

# Keep only the thinnest axial CT series
client.download(
    accession="11111111",
    output=Path("output/"),
    thinnest_axial=True,
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

# List an exam's series without downloading it. Pass the exam dict back
# verbatim; this queues no retrieval and transfers no image data.
exam, = client.search(accession="11111111", mrn="12345")
for s in client.list_series(exam):
    # imageCount counts DICOM objects, not frames
    print(s["seriesNumber"], s["description"], s["imageCount"])

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
