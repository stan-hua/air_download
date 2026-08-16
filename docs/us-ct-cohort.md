# Building a FAST-CT cohort

Pairing ED ultrasounds with the CTs that followed them, downloading the pair,
and converting both into arrays a model can load. This is the `us_ct/`
subpackage: `air_match`, `air_cohort`, and `air_convert`.

It assumes you already have two search-result CSVs — see
[Building a cohort, then downloading it](../README.md#building-a-cohort-then-downloading-it)
in the main README for how to produce them.

**The whole pipeline, in order:**

| Step | Command | Produces |
| --- | --- | --- |
| 1. Search | `pixi run search` | `accessions.csv`, one per modality |
| 2. Match | `pixi run match` | `matched_us_ct.csv`, one row per pair |
| 3. Download | `pixi run download-cohort` | `P0001/visit-01/{us,ct}/A0001.zip` + crosswalk |
| 4. Inspect | `pixi run frames inspect` | `frames.csv`, frame counts per instance |
| 5. Convert | `pixi run convert ct` / `us` | `.nii.gz` volumes and `.zarr` clips |
| 6. Sync *(optional)* | `rsync` | the arrays on a GPU host |

Jump to [**Running the pipeline**](#running-the-pipeline) for the commands.
Step 6 is only for people whose GPUs are on a different machine; if you train
where you downloaded, stop at step 5.

> **Everything past step 3 is pseudonymous.** No MRN, accession number, or
> visit date appears in any path or derived CSV. The crosswalk written beside
> the cohort is the only way back, and it is the one file to guard.

---

## Matching ultrasounds to the CTs that followed

`pixi run match` pairs two search-result CSVs into a cohort of patients who had an ED ultrasound and then a CT:

```bash
pixi run match --us_csv output-us_ed_bedside/accessions.csv \
               --ct_csv output-ct_abdomen_pelvis/accessions.csv \
               --output matched_us_ct.csv
```

A pair qualifies when all three hold:

1. **Same patient** — matched on `mrn`, never on accession number, which repeats across patients.
2. **CT strictly after the ultrasound** — a CT before it is excluded, and so is one sharing its timestamp, since neither followed the other.
3. **Within `--max_hours`** — 48 by default, inclusive at exactly the boundary; `--max_hours` changes the window.

Output is one row per matched ultrasound:

```
mrn,us_accession_number,us_date_time,us_description,us_image_count,ct_accession_number,ct_date_time,ct_description,ct_image_count,hours_between
A1,U1,2021-03-02T08:00:00-08:00,US ED BEDSIDE,22,C1,2021-03-02T14:00:00-08:00,CT ABDOMEN PELVIS W CONTRAST,480,6.0
A4,U4,2021-04-01T22:00:00-07:00,US ED BEDSIDE,18,C4,2021-04-02T03:00:00-07:00,CT ABDOMEN PELVIS W CONTRAST,512,5.0
```

`hours_between` is there so you can sanity-check the window and tighten it afterwards without re-running.

`us_image_count` and `ct_image_count` are carried straight through from the `image_count` column of the search results. They count [objects, not frames](../README.md#narrowing-to-the-exams-and-series-you-want), but they still separate a multi-view FAST from a single-view bedside scan. An older `accessions.csv` written before that column existed still matches; the two columns come out empty.

When a patient has **several qualifying CTs**, the default keeps the **earliest** one — the CT that actually followed the ultrasound. `--all_pairs` emits every qualifying CT instead:

```bash
pixi run match --us_csv us/accessions.csv --ct_csv ct/accessions.csv \
               --all_pairs --output matched_all.csv
```

### When several ultrasounds precede the same CT

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

**Timing is the only signal available at this stage.** Ranking on `us_image_count` was tried and removed: `image_count` counts DICOM *objects*, not frames, so a single-view study saved as twenty stills outranks a four-clip FAST. If the ultrasound you want is not the one nearest the CT, the honest answer is that the API cannot tell you which it is — settle it with real frame counts from [`pixi run frames`](../README.md#counting-frames-and-keeping-only-the-real-cine-clips) after downloading, then filter the matched CSV yourself.

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

## Downloading a matched cohort

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

### The crosswalk

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

## Converting to arrays a model can load

`pixi run convert` turns a downloaded cohort into per-exam files for PyTorch. The two halves need opposite operations, so they are separate commands.

```bash
pixi run convert ct --input output-cohort/ --output_dir output-arrays/
pixi run convert us --input output-cohort/ --output_dir output-arrays/ --min_frames 20 --grayscale True
```

Output mirrors the pseudonymous layout, so `P0001/visit-01/ct/A0002.zip` becomes `P0001/visit-01/ct/A0002.nii.gz` and still joins through the [crosswalk](#the-crosswalk).

**CT → one NIfTI per exam.** A CT arrives as a few hundred single-frame objects that are slices of one volume, so conversion *assembles*. Slices are ordered by `ImagePositionPatient` projected onto the slice normal — never by `InstanceNumber`, which scanners assign wrongly often enough to produce a silently shuffled volume. Pixels are rescaled to Hounsfield units and stored as int16, since HU are integers and float32 would double every volume to hold nothing.

NIfTI rather than Zarr is not a performance judgement: the open CT organ segmentors ([TotalSegmentator](https://pubs.rsna.org/doi/full/10.1148/ryai.230024) and relatives) are nnU-Net models that read and write NIfTI, so any other container means converting out and back. NIfTI rather than a bare `.npy` because `PixelSpacing` varies per patient while slice thickness does not — resampling to a common spacing needs the real millimetre geometry, and only the affine carries it.

A series whose slices disagree on size or orientation, or that holds two slices at one position, is refused rather than written as a volume that is quietly wrong.

**US → one Zarr array per clip.** An ultrasound arrives as cine clips of different views, so conversion *separates*. Each clip becomes `clip-<member_index>` in a Zarr group per exam, named to join straight to the `member_index` column of `frames.csv`. Three things happen per clip, in this order:

1. **Crop to `SequenceOfUltrasoundRegions`.** This is the de-identification step — see below.
2. **Mask and crop to the moving beamform**, via [ultraml](https://github.com/stan-hua/ultraml). The region box is conservative: it keeps depth markers, the `cm` label, and a small coloured vendor mark. The beamform mask is what removes them.
3. **Collapse to one channel.** `--grayscale True` forces it for a B-mode-only cohort; the default decides per clip so colour Doppler keeps its channels.

> **Ultrasound burns patient details into the banner around the image.** `BurnedInAnnotation` (0028,0301) is optional and routinely absent, so it cannot be used to decide whether that happened — assume it did. Cropping to `SequenceOfUltrasoundRegions`, the scanner's own statement of which pixels are image, removes it deterministically. That crop always runs **first**, and the beamform crop only ever runs inside it: mask growing follows connected bright pixels, so on a full frame it could in principle reach the banner, whereas after the crop there is no banner left to reach. An instance that declares no region is refused rather than falling back to the whole frame.

Arrays are chunked eight frames at a time by default, zstd level 9. That is measured, not guessed: one frame per chunk stores 38% of raw against 22% for eight, because a single frame is too small a window for zstd to find the redundancy consecutive cine frames obviously have. Reading one random frame costs about 3 ms, and a model fed a window of consecutive frames reads a whole chunk anyway. `--letterbox` raises the default to 32, since the frames it writes are much smaller — see below.

```python
import zarr
group = zarr.open_group("output-arrays/P0001/visit-01/us/A0001.zarr", mode="r")
clip = group["clip-0004"]          # (frames, height, width), uint8
frame = clip[7]                    # one frame, without touching the rest
clip.attrs["region_box"]           # what was cropped away, and why
```

### `--letterbox`: storing at the size a model actually reads

Native clips are large and non-square. Measured over a real 25-exam cohort: height 191–599, width 278–796, **median 729×598, about 1.25:1**. That shape is not noise — the beamform crop returns a tight box around the sector, and a sector is wider than it is deep.

`--letterbox 256` scales the long side to 256 and zero-pads the short one, giving uniform `(frames, 256, 256)` arrays that batch without a per-item resize. Measured across all 197 clips of the reference cohort, projected to a 5957-exam cohort:

| Stored as | Per exam | Whole cohort |
| --- | ---: | ---: |
| native | 118.8 MB | 708 GB |
| squashed to 256×256 | 17.2 MB | 103 GB |
| **letterboxed to 256×256** | **13.0 MB** | **77 GB** |

**Letterboxing is both cheaper and undistorted**, which is not the trade-off it looks like. Squashing a 1.25:1 clip into a square stretches anatomy by a quarter *and* invents 20% more real pixels to store, while the padding a letterbox adds compresses to almost nothing. The bars are not a new artifact either: everything outside the beamform is already zero, so they continue the background.

Two details worth knowing. A clip smaller than the target is padded but **never scaled up** — inventing detail to fill a frame costs space and buys nothing. And `chunk_frames` defaults to 32 rather than 8 when letterboxing, because a 256×256 frame is a fraction of the size and a chunk should stay a few megabytes: measured on the reference cohort, that is 49 files per exam instead of 160, for 2.6% less space. File count matters when the tree has to be copied to a GPU host.

`letterbox_scale` and `letterbox_offset` are recorded per clip, since the region and beamform boxes alone no longer locate a source pixel once a clip has been rescaled and centred.

### Verifying, and deleting the archive that was consumed

`air_convert` re-opens what it just wrote and checks it — the shape of a
volume, and one chunk of every clip. Nothing downstream re-reads these files
before the archive is deleted, so a truncated write, a full disk, or a
half-flushed gzip stream would all otherwise look exactly like success.

That check is what makes `--delete_source` safe, and the two together are what
let a full cohort run without ever holding more than one batch of archives.
See [Scaling up without filling the disk](#scaling-up-without-filling-the-disk).

On WSL, batching is not only a headroom question: the `ext4.vhdx` grows to its
high-water mark and **never shrinks on its own**, so downloading everything
before converting leaves the virtual disk permanently sized for archives that
no longer exist.

---

## Running the pipeline

### Getting a matched CSV

Search each modality into its own directory, then pair them. `--max_hours`
defaults to 48, so the flag is optional:

```bash
pixi run search -m CT -d "CT ABDOMEN PELVIS W CONTRAST" \
    -ds 2021-01-01 -de 2023-12-31 -o output-ct_abdomen_pelvis/
pixi run search -m US -d "US ED BEDSIDE" \
    -ds 2021-01-01 -de 2023-12-31 -o output-us_ed_bedside/

pixi run match --us_csv output-us_ed_bedside/accessions.csv \
               --ct_csv output-ct_abdomen_pelvis/accessions.csv \
               --output matched_us_ct.csv
```

### Developing against a handful of pairs

Work out your thresholds on a small cohort first. Either cut the matched CSV
down to a few rows, or keep the full one and let `--n` take the first few —
rows are sorted before `--n` slices them, so the same pairs are chosen and
numbered identically whether you take 25 or all of them. **The small run is
therefore not thrown away**: scaling up reuses every identifier already
assigned and skips every exam already fetched.

```bash
# Preview. No network call, nothing written, and the printed paths carry
# no identifiers, so they are safe to paste into a ticket.
pixi run download-cohort --matched_csv matched_us_ct-test.csv \
    --output cohort/ --dry_run

# Download.
pixi run download-cohort --matched_csv matched_us_ct-test.csv \
    --output cohort/ --cred_path ~/air_login.txt

# Look at the frame distribution before settling on a threshold. This only
# reports -- it filters nothing.
pixi run frames inspect --input cohort/ --output frames.csv --min_frames 20

# Convert. CT assembles into volumes, US separates into clips.
pixi run convert ct --input cohort/ --output_dir arrays/
pixi run convert us --input cohort/ --output_dir arrays/ \
    --min_frames 20 --grayscale True --letterbox 256
```

You now have `arrays/P0001/visit-01/{ct/A0002.nii.gz, us/A0001.zarr}` and a
crosswalk at `cohort_crosswalk.csv`, beside the tree rather than inside it.

### Scaling up without filling the disk

The archives are roughly five times the size of what they convert into, so a
full cohort should not keep them all at once. Downloading in batches and
converting after each one bounds peak disk at a single batch:

```bash
for limit in $(seq 200 200 6000); do
  pixi run download-cohort --matched_csv matched_us_ct.csv --output cohort/ \
      --converted_dir arrays/ --n $limit --cred_path ~/air_login.txt
  pixi run convert ct --input cohort/ --output_dir arrays/ --delete_source
  pixi run convert us --input cohort/ --output_dir arrays/ \
      --min_frames 20 --grayscale True --letterbox 256 --delete_source
done
```

`--converted_dir` is what makes this resumable. Without it, `--skip_existing`
looks for the `.zip` that `--delete_source` just removed, decides the exam was
never fetched, and downloads the whole cohort again.

Two flags do the work, and both are off by default because neither is
reversible without another download:

- **`--delete_source`** removes each archive once its output has been written
  **and re-opened**. `air_convert` verifies its own work — nothing downstream
  re-reads these files, so a truncated write would otherwise look exactly like
  success. A failed conversion keeps its archive, since at that point it is the
  only copy.
- **`--converted_dir`** skips an exam that already has a volume or a clip
  group, without a network call.

Interrupt it whenever you like; re-running picks up where it stopped.

> A CT matched to two ultrasounds is copied into both visits. Within one batch
> that copy is free; across batches the first visit's archive is already gone,
> so the CT is fetched a second time. On a real cohort that was ~90 exams out
> of ~12 000 — measured, and not worth a second code path.

### Optional: syncing to a GPU host

Only if you train somewhere other than where you downloaded. The converted
tree is the thing to move; the archives are already gone, and the crosswalk
must not travel:

```bash
rsync -a --info=progress2 arrays/ gpu-host:/data/fast_ct/arrays/
```

> **Never sync the crosswalk.** It is the only file that maps `P0001`/`A0001`
> back to a real patient, and a GPU host is usually a different trust
> boundary. Syncing `arrays/` reaches it only if you put it there, which is
> why `air_cohort` refuses a crosswalk path inside the cohort in the first
> place. Everything the training job needs joins on `anon_mrn` and
> `anon_accession_number` alone.

Ultrasound at `--letterbox 256` is about 13 MB per exam, so a 6000-pair cohort
is roughly 77 GB over the wire. CT volumes are about 66 MB each; if the GPU
host only needs embeddings, run the segmentation and encoder there in batches
and keep the volumes at home rather than syncing ~390 GB.

### What the reference cohort produced

Measured on 25 pairs, and the per-exam figures are what to multiply when
planning storage for a larger cohort.

| | |
| --- | --- |
| Patients | 25 |
| Archives | 50 (25 US, 25 CT), 4.3 GB — deleted after conversion |
| CT | 195-319 slices each, 512x512, 2 mm, one series per exam |
| US | 203 instances, 197 of them clips of 20+ frames |
| Frames | 6107 CT slices, 29 834 ultrasound frames |
| CT volumes | 66 MB per exam (55-90) |
| US clips, native | 119 MB per exam |
| US clips, `--letterbox 256` | **13 MB per exam**, 49 files |

At those rates a 6000-pair cohort is about 390 GB of CT and 77 GB of
ultrasound. The archives that produced them would have been over a terabyte,
which is the argument for `--delete_source`.

**Why `--min_frames 20`.** The distribution splits cleanly. Across 6107
instances, nothing at all fell between 2 and 19 frames: an instance is either a
single still or a clip of at least 20. Any threshold in that gap gives an
identical result, so 20 sits at the bottom of it and discards no real clip.
Raising it to 60 would drop 5 genuine clips for nothing.

**Why `--grayscale True`.** Every clip in this cohort is B-mode, stored as
three identical channels in an RGB container. Forcing one channel is lossless
here and a third of the size. Drop the flag for a cohort that might contain
colour Doppler, and it will decide per clip instead.
