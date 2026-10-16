# scan-organizer

OCR scanned documents, classify them against a YAML taxonomy, file them under
`{owner}/{area}/{doc_type}/{short_name}_{YYYY-MM-DD}.{ext}`, and keep a
replayable JSONL manifest of every move so you can undo it.

Built for a hands‑off weekly batch: runs in Docker on a Debian host that mounts
the Synology scans share over NFS, triggered by cron or a systemd timer.

---

## Table of contents

- [What's new](#whats-new)
- [How it decides where a file goes](#how-it-decides-where-a-file-goes)
- [Install (local Python)](#install-local-python)
- [CLI reference](#cli-reference)
- [Interactive mode](#interactive-mode)
- [Privacy](#privacy)
- [Weekly run in Docker on a Debian host](#weekly-run-in-docker-on-a-debian-host)
- [Weekly schedule (cron OR systemd timer)](#weekly-schedule-cron-or-systemd-timer)
- [Re-filing legacy folders](#re-filing-legacy-folders)
- [Undo a run](#undo-a-run)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## What's new

`scan_organizer.py` and `scan_organizer_secure.py` have been merged into a
single entry point driven by CLI flags. All of the previous features are still
reachable, plus:

- Taxonomy lives in [`taxonomy.yaml`](taxonomy.yaml). Classification results are
  validated against it; anything invalid or low‑confidence lands in `_Review/`
  — never in a brand‑new folder.
- Classifier choice: `--provider anthropic|gemini|local-keyword`.
- A **local privacy filter** runs before any outbound call. On a sensitive match
  the file is classified locally and no text leaves the host. Documents that
  are sent out get SSNs, long account numbers, and DOBs scrubbed.
- JSONL manifest of every move + `--undo <manifest>` to reverse a batch.
- Re‑file mode: `--refile <dir>` promotes files from legacy flat folders into
  the new taxonomy (dry‑run capable).
- `--dry-run`, `--limit`, `--non-interactive`.
- Weekly `summary-YYYY-MM-DD.md` with per‑folder counts and the `_Review` list.
- `Dockerfile`, `docker-compose.yml`, cron + systemd‑timer samples.
- `test_api_key.py` no longer prints any portion of the API key (supersedes
  [PR #1](https://github.com/etawiah/scan-organizer/pull/1)).

---

## How it decides where a file goes

For each file in `--src`:

1. Compute `sha256`. If we've already filed a file with this hash
   (per manifests in `--log`), skip it as a duplicate.
2. `.docx` and other review‑only formats go straight to `_Review/`.
3. OCR up to `--ocr-pages` pages (default `5`). Empty text → `_Unsorted/`.
4. The **privacy filter** matches the OCR text against your sensitive terms.
   On a match, classify locally with keyword rules and send nothing out.
5. Otherwise redact SSNs / long account numbers / DOBs, then call the chosen
   `--provider` for a JSON classification:
   `{owner, area, doc_type, doc_date, confidence, short_name}`.
6. The result is validated against `taxonomy.yaml`.
   - Not in the taxonomy → `_Review/`.
   - `confidence < confidence_threshold` (default 0.75) → `_Review/`.
7. Final document date = latest plausible date in the text,
   else the `MMDDYYYY` embedded in a Brother‑style scanner filename
   (`scan10162026_20261016000724.pdf`), else the file's `mtime`.
8. Move to
   `{dest}/{owner}/{area}/{doc_type}/{short_name}_{YYYY-MM-DD}.{ext}`.
   (Owners that have no `area` layer — currently just `Personal` — collapse to
   `{dest}/Personal/{doc_type}/…`.)
9. Append a line to the manifest describing the move.

A per‑file crash never stops the batch — the file is routed to `_Review/` and
the run continues.

---

## Install (local Python)

System dependencies:

```bash
sudo apt-get install -y tesseract-ocr poppler-utils
```

Python dependencies:

```bash
pip install -r requirements.txt
```

That's it. Run with no arguments for interactive mode, or pass flags for the
hands‑off path.

---

## CLI reference

```
scan_organizer.py [options]
```

Paths:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--scans-root PATH` | `/scans` or `$SCANS_ROOT` | Root of the NFS mount. |
| `--src PATH` | `<scans-root>/Unsorted Docs` | Inbox folder with files to process. |
| `--dest PATH` | `<scans-root>` | Root under which taxonomy folders are written. |
| `--review-dir PATH` | `<dest>/_Review` | Low‑confidence / invalid files land here. |
| `--unsorted-dir PATH` | `<dest>/_Unsorted` | Empty‑OCR files land here. |
| `--log PATH` | `<dest>/_logs` | JSONL manifests + summaries. |
| `--taxonomy PATH` | `./taxonomy.yaml` | YAML taxonomy. |
| `--private-terms PATH` | `./private_terms.txt` if it exists | Local sensitive terms. |

Behavior:

| Flag | Purpose |
| --- | --- |
| `--provider {anthropic,gemini,local-keyword}` | Classifier. Default: `$SCAN_PROVIDER` or `local-keyword`. |
| `--model NAME` | Model override for the chosen provider. |
| `--confidence-threshold FLOAT` | Overrides the YAML threshold. |
| `--ocr-pages N` | PDF pages to OCR. Default `5`. |
| `--dry-run` | Write the manifest and print the plan; move nothing. |
| `--limit N` | Process at most N files (good for incremental backfills). |
| `--non-interactive` | Never prompt. Required for Docker / cron / systemd. |
| `--recursive` | Descend into subdirectories of `--src`. |
| `--refile DIR` | Re‑classify files already in a legacy folder (repeatable). |
| `--undo MANIFEST` | Reverse the moves recorded in `MANIFEST` and exit. |
| `--summary-only` | Skip processing; just rewrite today's summary from `--log`. |
| `--no-summary` | Don't write the weekly summary at the end of a run. |
| `-v` / `-vv` | More logging. |

Environment variables honored:

- `SCANS_ROOT` — default for `--scans-root`.
- `SCAN_PROVIDER` — default for `--provider`.
- `SCAN_MODEL` — default for `--model`.
- `ANTHROPIC_API_KEY` — required when `--provider anthropic`.
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) — required when `--provider gemini`.

---

## Interactive mode

`python3 scan_organizer.py` with no arguments and a TTY prompts for scans root,
source folder, provider and dry‑run choice, then delegates to the same
non‑interactive code path. The original interactive walk is preserved — just
upgraded to use the new taxonomy.

---

## Privacy

Create a local `private_terms.txt` (git‑ignored) from the committed example:

```bash
cp private_terms.example.txt private_terms.txt
$EDITOR private_terms.txt
```

Every line is a case‑insensitive term. Blank lines and `# comments` are
ignored. Common minor/medical keywords (`school`, `pediatric`, `report card`,
`immunization`, `DOB`, `IEP`, …) are built in and don't need to be repeated.

**Guarantee:** when ANY of these terms appears in the OCR'd text, the external
provider is never called for that document. It is classified locally with
keyword rules and routed through the normal taxonomy / `_Review/` flow.

For everything else, the text sent to the provider is first scrubbed for:

- US SSNs (`123-45-6789`)
- Long account numbers (10+ consecutive digits, or 11–24 digits with spaces/
  dashes between every digit — typical card formats)
- Dates of birth labeled with `DOB`, `D.O.B.`, `date of birth`, or `born on …`

---

## Weekly run in Docker on a Debian host

> The container does **not** run on the Synology. It runs on a Debian Docker
> host that mounts the Synology share over NFS. Volumes are bind‑mounted
> read‑write so the organizer can move files inside the share.

1. Mount the share once in `/etc/fstab`:

   ```fstab
   nas.local:/volume1/scans  /mnt/nas/scans  nfs  defaults,_netdev,soft  0 0
   ```

   Reload: `sudo systemctl daemon-reload && sudo mount -a`.
   Confirm: `mountpoint -q /mnt/nas/scans && echo OK`.

2. Prepare host config (keeps your taxonomy + sensitive terms outside the
   image):

   ```bash
   sudo mkdir -p /opt/scan-organizer/config
   sudo cp taxonomy.yaml                 /opt/scan-organizer/config/
   sudo cp private_terms.example.txt     /opt/scan-organizer/config/private_terms.txt
   sudo $EDITOR /opt/scan-organizer/config/private_terms.txt
   ```

3. Create `/etc/default/scan-organizer` (root‑only, holds secrets):

   ```ini
   SCANS_HOST_PATH=/mnt/nas/scans
   CONFIG_HOST_PATH=/opt/scan-organizer/config
   SCAN_PROVIDER=anthropic
   ANTHROPIC_API_KEY=sk-ant-...
   TZ=America/New_York
   ```

   ```bash
   sudo chmod 600 /etc/default/scan-organizer
   ```

4. Build:

   ```bash
   sudo cp -r . /opt/scan-organizer
   cd /opt/scan-organizer && sudo docker compose build
   ```

5. Dry‑run first:

   ```bash
   set -a; . /etc/default/scan-organizer; set +a
   docker compose run --rm scan-organizer --dry-run -v
   ```

   Review `/mnt/nas/scans/_logs/manifest-*.jsonl` and the generated
   `summary-YYYY-MM-DD.md`.

6. Real run:

   ```bash
   set -a; . /etc/default/scan-organizer; set +a
   docker compose run --rm scan-organizer
   ```

---

## Weekly schedule (cron OR systemd timer)

Pick one.

### cron

Sample at [`cron.example`](cron.example). Install:

```bash
sudo cp cron.example /etc/cron.d/scan-organizer
sudo chmod 644 /etc/cron.d/scan-organizer
```

Default schedule: Sunday 03:17.

### systemd timer

Sample unit + timer at [`systemd/scan-organizer.service`](systemd/scan-organizer.service)
and [`systemd/scan-organizer.timer`](systemd/scan-organizer.timer). Install:

```bash
sudo cp systemd/scan-organizer.service /etc/systemd/system/
sudo cp systemd/scan-organizer.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scan-organizer.timer
```

The service refuses to start if `/mnt/nas/scans` is not currently a mount
point, so an off‑line NAS can't cause a partial run.

---

## Re-filing legacy folders

The real share layout has two legacy areas plus the pre‑existing per‑category
folders. Point `--refile` at as many as you need:

```bash
docker compose run --rm scan-organizer \
  --refile "/scans/Labeled-Unsorted" \
  --refile "/scans/Tax Docs" \
  --refile "/scans/MAB Docs" \
  --refile "/scans/Family Docs" \
  --dry-run
```

`--refile` recursively walks each folder you pass, skipping `#recycle`,
`@eaDir`, dot‑files, and `.DS_Store`. Review the manifest, then re‑run without
`--dry-run`.

Legacy flat category folders from the earlier version (`receipts/`,
`invoices/`, `bills/`, `letters/`, `documents/`, `mail/`, `pictures/`, `other/`)
are supported too.

---

## Undo a run

```bash
docker compose run --rm scan-organizer \
  --undo /scans/_logs/manifest-20261018T031700Z.jsonl
```

Dry‑run first with `--dry-run --undo …`. The undo reads the manifest from
newest to oldest, verifies each destination still exists and still matches the
recorded `sha256`, then moves it back to its original source path.

---

## Testing

```bash
pip install pytest pyyaml
pytest -q
```

The test suite covers:

- `tests/test_classification.py` — local keyword classifier over representative
  OCR samples, plus the fallback path when a remote provider's env var is
  missing.
- `tests/test_privacy.py` — sensitive term matching with built‑in and user‑
  loaded terms, outbound gating decision.
- `tests/test_redaction.py` — SSN / long account number / DOB redaction.
- `tests/test_undo.py` — end‑to‑end round trip: seed an inbox, process, undo,
  verify everything is back where it started, plus the duplicate‑skip path.
- `tests/test_dry_run.py` — dry‑run writes a manifest but moves nothing,
  empty OCR routes to `_Unsorted/`, `.docx` routes to `_Review/` without
  invoking OCR or any provider, and `#recycle` / `@eaDir` / `.DS_Store` are
  skipped.
- `tests/test_dates.py` — document date resolution precedence.
- `tests/test_taxonomy.py` — YAML schema and node validation.

Tests use a monkey‑patched OCR shim so they run without Tesseract or Poppler.

---

## Troubleshooting

- **`ANTHROPIC_API_KEY not set`** when `--provider anthropic`: export the key
  before invoking the container, or put it in `/etc/default/scan-organizer`.
- **Everything lands in `_Review/`**: either the classifier confidence is below
  the YAML threshold, or the model returned an owner/area/doc_type that is not
  in `taxonomy.yaml`. Inspect the latest `manifest-*.jsonl` — the full
  classification payload is recorded on every line.
- **Everything lands in `_Unsorted/`**: OCR produced empty text. Verify
  `tesseract --version` and `pdftoppm -v` inside the container, and check the
  resolution of your scans.
- **`ConditionPathIsMountPoint` fails** on the systemd timer: the NFS mount
  isn't up. Fix the mount first; the next scheduled run will fire thanks to
  `Persistent=true`.
- **Scanner clock is UTC** — Brother‑style filenames
  (`scan10162026_20261016000724.pdf`) are parsed as UTC and only used as a
  fallback when neither the OCR text nor an explicit `doc_date` from the
  classifier yields a plausible date.

---

Platform‑specific setup notes for Windows and macOS live in
[`WINDOWS_SETUP.md`](WINDOWS_SETUP.md).
