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
- Classifier choice: `--provider anthropic|gemini|local-keyword|ollama`.
  The Anthropic and Gemini defaults are deliberately cheap hosted tiers
  (`claude-3-haiku-20240307` and `gemini-2.5-flash-lite`). Gemini 2.0
  Flash shut down in June 2026. Keyword-only Gemini is a poor fit for
  this corpus, so `--provider ollama` talks to a **local** model on
  the LAN (`SCAN_PROVIDER=ollama`, `OLLAMA_URL` default
  `http://localhost:11434`, deploy on `192.168.3.x`,
  `OLLAMA_MODEL` default `qwen2.5:7b-instruct`, `OLLAMA_TIMEOUT`
  default 180s / minimum 120s). The host handles **one request at a
  time**; calls are serialized and the OCR text sent per file is
  capped at about 2500 tokens (~10k characters). Ollama is marked
  `is_local=True`: it receives the **full OCR** (private terms, kids'
  names, and `LOCAL_ONLY_CATEGORIES` included) because nothing leaves
  the house. Hits are still **counted** and logged as counts only.
  `OLLAMA_URL` is refused unless the host is `localhost` or an
  RFC1918/LAN address. Ollama never falls back to Gemini or Anthropic:
  an error or timeout sends the file to `_Review/`. Cloud providers
  keep the full gate (private-term / kids / SSN / local-only /
  allowlist) unchanged.
- A **single local redaction gate** runs before any outbound call. It
  guarantees that no children's PII — and no SSN, EIN, routing, account,
  or card number — ever reaches a frontier model. Any private-term hit
  short-circuits a **cloud** provider to the local classifier
  (`local-only: sensitive`). Ollama still receives the full OCR.
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
- `--prompts-only --dry-run` writes the exact final outbound prompt for
  each file that would hit a provider to `prompts-<UTC timestamp>.jsonl`
  (mode `0600`) and makes zero network calls. `--print-prompts` writes
  the same file and then continues to classify.
- `--max-cloud-calls N` (`SCAN_MAX_CLOUD_CALLS`) counts every HTTP
  request, including retries, and sends remaining outbound files to
  `_Review/` when the cap is reached. `--no-retry` /
  `SCAN_GEMINI_RETRIES` (default 3) controls Gemini 429/503 retries.
  Local Ollama calls are not counted.
- Every model answer is **normalized** (trim, drop blank path
  segments, collapse consecutive repeats, case-insensitive match)
  against `taxonomy.yaml`. Empty, malformed (`A//A/B`,
  `Finance/Finance`), or below-threshold paths go to `_Review/` and
  are never filed — for every provider.
- OCR retries a low-confidence or low word-hit-rate page with a
  horizontal flip and a 180° rotation, and keeps the best text
  (mirrored / flipped ADF scans).
- Per-file runtime is logged and the average is printed on the
  console and in `summary-<UTC timestamp>.md`.
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
3. OCR up to `--ocr-pages` pages (default `5`). If Tesseract
   confidence or the word-hit rate is low, the same page is retried
   horizontally flipped and rotated 180°, and the best text is kept.
   Empty text → `_Unsorted/`.
4. The **redaction gate** processes the OCR text:
   * Private terms from `config/private_terms.txt` or `PRIVATE_TERMS`
     (whole-word, case-insensitive including ALL CAPS, `First Last` /
     `Last, First`, and lone given-name tokens; `_` / `-` count as
     boundaries in filenames and paths) are counted and replaced with
     `[REDACTED_TERM]`. For a **cloud** provider any hit flips the
     document to `local-only: sensitive`. For **ollama** the hit is
     counted only; the full OCR still goes to the LAN model.
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
5. For non-sensitive documents a **cloud** provider sees an
   **allowlist prompt** only: taxonomy-category names, keywords from
   the committed generic vocabulary (`config/outbound_vocabulary.txt`)
   that appear in the document, and coarse features (page bucket,
   has-dates, has-amounts). OCR text, filenames, paths, numbers,
   names, emails, and addresses never leave the host.
   `assert_safe_for_outbound` requires every token to be in that
   vocabulary or the taxonomy. Documents whose local keyword pre-pass
   is `Finance-Bank`, `ID-Legal`, or `Medical` stay local-only;
   override with `LOCAL_ONLY_CATEGORIES`. The **ollama** provider is
   local (`is_local=True`) so the allowlist gate does not apply: it
   receives the **full OCR** (including private-term / kids' /
   local-only documents), capped at ~2500 tokens (~10k characters),
   one file at a time. Private-term hits are still counted in the
   manifest (counts only). If Ollama errors or times out the file
   goes to `_Review/` — never to Gemini or Anthropic.
6. The result is validated against `taxonomy.yaml` after the path is
   normalized (trim, collapse blank or repeated segments,
   case-insensitive).
   - Not in the taxonomy, empty, or malformed → `_Review/`, never filed.
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
| `--provider {anthropic,gemini,local-keyword,ollama}` | Classifier. Default: `$SCAN_PROVIDER` or `local-keyword`. |
| `--model NAME` | Model override for the chosen provider. |
| `--confidence-threshold FLOAT` | Overrides the YAML threshold. |
| `--ocr-pages N` | PDF pages to OCR. Default `5`. |
| `--dry-run` | Write the manifest and print the plan; move nothing. |
| `--print-prompts` | Write `prompts-<UTC timestamp>.jsonl` (mode 0600) with the exact final outbound prompt, then classify normally. |
| `--prompts-only` | Requires `--dry-run`. Write the same prompt dump and make zero network calls. |
| `--max-cloud-calls N` | Cap HTTP requests (including retries). Default `$SCAN_MAX_CLOUD_CALLS` or unlimited. Remaining outbound files go to `_Review/`. |
| `--no-retry` | Do not retry Gemini 429/503. Same as `SCAN_GEMINI_RETRIES=0` (default 3). |
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
- `SCAN_MAX_CLOUD_CALLS` — cap HTTP requests (including retries).
  Unset = unlimited. Remaining outbound files go to `_Review/`.
- `SCAN_GEMINI_RETRIES` — Gemini 429/503 retries after the first
  attempt (default 3). `0` is the same as `--no-retry`.
- `ANTHROPIC_API_KEY` — required when `--provider anthropic`.
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) — required when `--provider gemini`.
- `OLLAMA_URL` — Ollama base URL (default `http://localhost:11434`).
  Host must be `localhost` or an RFC1918/LAN address (the live
  daemon is on `192.168.3.x`); anything else is refused. Example in
  [`.env.example`](.env.example): `http://192.168.x.x:11434`.
- `OLLAMA_MODEL` — Ollama model id (default `qwen2.5:7b-instruct`).
- `OLLAMA_TIMEOUT` — seconds (default `180`, minimum `120`). The
  model handles one request at a time; OCR text is capped at ~2500
  tokens (~10k characters) per file.

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

* **No children's PII to any cloud LLM (Gemini / Anthropic).** Any
  private‑term hit, or two or more hits against the built‑in topical
  signal list (SSN / tax / medical / school‑record keywords), causes
  the document to stay on-host and be recorded as
  `local-only: sensitive`. Zero cloud provider calls are made.
  **Ollama is the exception:** a validated LAN / loopback URL never
  leaves the house, so private-term hits, kids' names, and
  `LOCAL_ONLY_CATEGORIES` are sent to the local model with the full
  OCR. Hits are still counted and logged as counts only.
* **No SSN, EIN, routing, account, or card number to any frontier model.**
  Documents that *are* sent to a **cloud** provider first go through the
  single redaction gate, which scrubs:
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

A defensive residual‑PII check inside every **cloud** provider call
re‑scans the final prompt immediately before it goes on the wire and
refuses to transmit anything that still matches an ID pattern.
Ollama does not use that allowlist (and never falls back to a cloud
provider if the local call fails).

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
- `tests/test_print_prompts.py` — `--prompts-only` requires `--dry-run`
  and never calls `urlopen`; `--print-prompts` dumps prompts then
  classifies.
- `tests/test_cloud_calls.py` — `--max-cloud-calls` counts retries;
  remaining outbound files go to `_Review/`.
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
- `tests/test_taxonomy.py` — YAML schema, node validation, and path
  normalization (`A//A/B`, empty, `Finance/Finance` → `_Review/`).
- `tests/test_ollama.py` — local provider, LAN URL guard, OCR cap,
  sequential calls, low-confidence → `_Review/`.
- `tests/test_ollama_local_policy.py` — ollama gets full private-term
  OCR; the same file under gemini never reaches the provider; ollama
  errors go to `_Review/` and never call Gemini/Anthropic. An invalid
  or non-LAN `OLLAMA_URL` keeps private-term and local-only files
  `privacy_blocked`; `--prompts-only` never dumps their unredacted OCR.
- `tests/test_ocr.py` — mirrored/flipped synthetic image keeps the
  best orientation.
- `tests/test_runtime.py` — per-file elapsed time and summary average.

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
