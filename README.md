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
- Classifier choice: `--provider anthropic|gemini|local-keyword`. The
  Anthropic and Gemini defaults are deliberately cheap hosted tiers
  (`claude-3-haiku-20240307` and `gemini-2.5-flash-lite`). Gemini 2.0
  Flash shut down in June 2026.
- A **single local redaction gate** runs before any outbound call. It
  guarantees that no children's PII — and no SSN, EIN, routing, account,
  or card number — ever reaches a frontier model. Any private-term hit
  short-circuits to the local classifier (`local-only: sensitive`).
  Medical / injury keywords force local-only regardless of category.
  Outbound prompts are **allowlist-only**: never OCR text, filenames, or
  paths — only taxonomy names, committed generic vocabulary hits, and
  coarse features. A defensive token check inside each provider call
  refuses anything outside that allowlist.
- JSONL manifest of every move + `--undo <manifest>` to reverse a batch.
- Re‑file mode: `--refile <dir>` promotes files from legacy flat folders into
  the new taxonomy (dry‑run capable).
- `--dry-run`, `--limit`, `--non-interactive`.
- Per-run `summary-<UTC timestamp>.md` (same stamp scheme as the manifest)
  with per‑folder counts and the `_Review` list. Runs never overwrite each
  other.
- `--print-prompts --dry-run` writes the exact final outbound prompt for
  each file that would hit a provider to `prompts-<UTC timestamp>.jsonl`
  (mode `0600`) and makes zero network calls.
- `--provider-probe` sends a content-free `ping` and prints HTTP status.
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
4. The **redaction gate** processes the OCR text:
   * Private terms from `config/private_terms.txt` or `PRIVATE_TERMS`
     (whole-word, case-insensitive including ALL CAPS, `First Last` /
     `Last, First`, and lone given-name tokens; `_` / `-` count as
     boundaries in filenames and paths) are counted and replaced with
     `[REDACTED_TERM]`. Any hit flips the document to
     `local-only: sensitive`.
   * SSNs (`123-45-6789`, spaced, and 9-digit unformatted near SSN
     keywords), EINs (`##-#######`), account/routing numbers (digit runs
     of 8+ allowing spaces/dashes), payment cards (Luhn-valid 13–19 digit
     runs tagged `[REDACTED_CARD]`), labeled DOBs, **card/account
     4-digit fragments** (including visible last-4 such as `****1234`),
     **US street addresses** (case-insensitive, including PO Box),
     **emails**, **VINs**, **title / lien numbers**, **law-firm names**,
     and **Title-Case person-name runs** are scrubbed. Fragments /
     addresses / names use typed placeholders `[CARD]`, `[ADDRESS]`,
     `[NAME]`.
   * The same pass is applied to OCR text, filenames, and paths.
   * Medical / injury keywords (`diagnosis`, `injury`, `claim`,
     `physician`, `hospital`, `prescription`, `accident`, …) force
     `local-only` regardless of the local taxonomy category.
   * The scrubbed text is re-scanned for residual PII shapes and for
     topical signals (SSN / tax / medical / school keywords). A hit on
     either keeps the document local.
   * The gate reports counts only — never the matched values — so logs
     and manifests never carry private strings.
5. For non-sensitive documents the provider sees an **allowlist prompt**
   only: taxonomy-category names, keywords from the committed generic
   vocabulary (`config/outbound_vocabulary.txt`) that appear in the
   document, and coarse features (page bucket, has-dates, has-amounts).
   OCR text, filenames, paths, numbers, names, emails, and addresses
   never leave the host. `assert_safe_for_outbound` requires every
   token to be in that vocabulary or the taxonomy. Documents whose
   local keyword pre-pass is `Finance-Bank`, `ID-Legal`, or `Medical`
   stay local-only; override with `LOCAL_ONLY_CATEGORIES`.
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
| `--print-prompts` | Requires `--dry-run`. Write `prompts-<UTC timestamp>.jsonl` (mode 0600) with the exact final outbound prompt for each file that would go to a provider. Zero network calls. |
| `--provider-probe` | Send a content-free `ping` to `--provider`, print status, exit. |
| `--limit N` | Process at most N files (good for incremental backfills). |
| `--non-interactive` | Never prompt. Required for Docker / cron / systemd. |
| `--recursive` | Descend into subdirectories of `--src`. |
| `--refile DIR` | Re‑classify files already in a legacy folder (repeatable). |
| `--undo MANIFEST` | Reverse the moves recorded in `MANIFEST` and exit. |
| `--summary-only` | Skip processing; just write a new `summary-<UTC timestamp>.md` from `--log`. |
| `--no-summary` | Don't write the summary at the end of a run. |
| `-v` / `-vv` | More logging. |

Environment variables honored:

- `SCANS_ROOT` — default for `--scans-root`.
- `SCAN_PROVIDER` — default for `--provider`.
- `SCAN_MODEL` — generic default for `--model` (applies to whichever
  provider is active unless a provider‑specific override is also set).
- `SCAN_ANTHROPIC_MODEL` — override for the Anthropic model.
  Defaults to `claude-3-haiku-20240307` (older, cheap Haiku tier).
- `GEMINI_MODEL` / `SCAN_GEMINI_MODEL` — override for the Gemini model.
  Defaults to `gemini-2.5-flash-lite` (oldest cheap Flash still listed
  as live; 2.0 is shut down). `GEMINI_MODEL` wins.
- `PRIVATE_TERMS` / `SCAN_PRIVATE_TERMS` — extra private terms (comma or
  newline separated). Same matching as the gitignored
  `private_terms.txt`. Never commit real names.
- `LOCAL_ONLY_CATEGORIES` — comma-separated doc_types that stay
  on-host (default `Finance-Bank,ID-Legal,Medical`). Empty disables.
- `ANTHROPIC_API_KEY` — required when `--provider anthropic`.
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) — required when `--provider gemini`.

Copy [`.env.example`](.env.example) to `.env` (git‑ignored) and fill the
values in locally; or set these variables in
`/etc/default/scan-organizer` for cron / systemd.

---

## Interactive mode

`python3 scan_organizer.py` with no arguments and a TTY prompts for scans root,
source folder, provider and dry‑run choice, then delegates to the same
non‑interactive code path. The original interactive walk is preserved — just
upgraded to use the new taxonomy.

---

## Privacy

Create a local `config/private_terms.txt` (git‑ignored) from the committed
example:

```bash
mkdir -p config
cp config/private_terms.example.txt config/private_terms.txt
$EDITOR config/private_terms.txt
```

Legacy `private_terms.txt` in the repo root is still honored for backward
compatibility; the new `config/private_terms.txt` location takes
precedence when both exist.

Every line is a term. Matching is case‑insensitive (including ALL
CAPS) with whole‑word boundaries; `_` and `-` are boundaries so
`ZEBULON_scan.pdf` matches. Two‑word names are also matched in
`Last, First` order **and** as the lone given name (`Zebulon Quill`
also catches `ZEBULON` / `zebulon` alone). Blank lines and
`# comments` are ignored. You can also pass terms via the
`PRIVATE_TERMS` env var (comma or newline separated) so nothing
sensitive has to live in git. Common minor/medical keywords
(`school`, `pediatric`, `report card`, `immunization`, `DOB`,
`IEP`, …) are built in and don't need to be repeated.

### Owner rule — enforced by code

* **No children's PII to any LLM.** Any private‑term hit, or two or more
  hits against the built‑in topical signal list
  (SSN / tax / medical / school‑record keywords), causes the document to
  be classified locally and recorded in the manifest as
  `local-only: sensitive`. Zero provider calls are made.
* **No SSN, EIN, routing, account, or card number to any frontier model.**
  Documents that *are* sent to the provider first go through the single
  redaction gate, which scrubs:
  * US SSNs — `123-45-6789`, `123 45 6789`, and unformatted
    9‑digit values when they sit next to an SSN / TIN / ITIN cue.
  * EINs — `##-#######`.
  * Account/routing numbers — any digit run of 8+, including runs
    separated by spaces or dashes (groups of 3+ digits).
  * Payment cards — Luhn‑valid 13–19 digit sequences, tagged as
    `[REDACTED_CARD]`.
  * Card / account 4-digit fragments (`ending in 1234`, `xxxx1234`,
    `acct #1234`, `last 4: 1234`, …) tagged `[CARD]`.
  * US street addresses (number + name + suffix, optional suite/apt,
    city/state/ZIP, and state+ZIP) tagged `[ADDRESS]`.
  * Title-Case 2–3 token person names, honorifics, and `Last, First`,
    tagged `[NAME]`. Common document / org words (months, states,
    Bank, Insurance, Statement, Internal Revenue Service, …) are
    allowlisted so category signal survives.
  * Dates of birth labeled with `DOB`, `D.O.B.`, `date of birth`,
    `birthdate`, `birthday`, or `born on …`.
* **Nothing sensitive is ever logged.** The gate reports counts and
  category labels only — never the matched substring or term.

A defensive residual‑PII check inside every provider call re‑scans the
final prompt immediately before it goes on the wire and refuses to
transmit anything that still matches an ID pattern.

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
   image). The container searches `$SCAN_CONFIG_DIR`, then `/config`
   (the compose mount), then `/app/config` (baked-in vocabulary and
   taxonomy), then `./config`. Host files win; missing files fall
   through. The outbound vocabulary is copied into the image so a
   host mount that omits it no longer fails closed at startup.

   ```bash
   sudo mkdir -p /opt/scan-organizer/config
   sudo cp taxonomy.yaml                        /opt/scan-organizer/config/
   sudo cp config/private_terms.example.txt     /opt/scan-organizer/config/private_terms.txt
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
   `summary-<UTC timestamp>.md`.

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
- `tests/test_privacy.py` — private‑term matching, `Last, First` variant,
  whole‑word boundary, outbound gating and count‑only reporting.
- `tests/test_redaction.py` — redaction of each category (SSN dashed /
  spaced / near-keyword, EIN, long account digits, Luhn-valid card,
  spaced/dashed routing, DOB variants), Luhn checksum, and the
  `assert_safe_for_outbound` defensive guard.
- `tests/test_identity_redaction.py` — card fragments, US addresses,
  Title-Case names, allowlist, and the two dry-run leak shapes
  (`Zebulon Quill` + `card ending 0000`; names + `123 Fakeville Rd…`).
- `tests/test_local_only_categories.py` — `LOCAL_ONLY_CATEGORIES` default
  and override; Finance-Bank / ID-Legal / Medical stay on-host.
- `tests/test_summary_stamp.py` — `summary-<UTC timestamp>.md` never
  overwrites a sibling run.
- `tests/test_gemini_retry.py` — `GEMINI_MODEL`, 503/429 backoff,
  ProviderError logs status+body (not the prompt), `--provider-probe`.
- `tests/test_print_prompts.py` — `--print-prompts` requires `--dry-run`,
  writes mode-0600 JSONL, and never calls `urlopen`.
- `tests/test_allowlist_outbound.py` — allowlist prompts and fake-data
  regressions for every leftover leak shape (ALL CAPS / PO Box
  addresses, last-4, VIN, surname email, law firm, medical text in a
  non-Medical doc).
- `tests/test_outbound_privacy.py` — Anthropic and Gemini providers are
  mocked and the test asserts that no SSN, EIN, account, card, DOB, or
  private‑term value ever appears in the outbound prompt, logs, or
  manifest. Also exercises the `local-only: sensitive` short‑circuit for
  private‑term hits and the topic‑density fallback.
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
