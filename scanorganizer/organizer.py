"""Batch orchestration: process_file, process_batch, refile_folder."""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, TextIO

from . import classifier as classifier_mod
from . import ocr as ocr_mod
from .classifier import ClassifierSettings
from .config import REVIEW_ONLY_EXTS, is_local_only_category
from .dates import resolve_doc_date
from .hashing import sha256_file
from .manifest import ManifestEntry, ManifestWriter, seen_hashes
from .naming import build_dest_path, build_filename, disambiguate, sanitize_short_name
from .privacy import (
    RedactionReport,
    SensitiveTerms,
    assert_safe_for_outbound,
    gate,
)
from .vocabulary import extract_outbound_signals
from .scanner import CandidateFile, walk_inbox
from .taxonomy import Taxonomy, TaxonomyNode

log = logging.getLogger(__name__)


@dataclass
class RunConfig:
    src: Path
    dest: Path
    review_dir: Path
    unsorted_dir: Path
    manifest_path: Path
    taxonomy: Taxonomy
    settings: ClassifierSettings
    sensitive_terms: SensitiveTerms
    dry_run: bool = False
    limit: Optional[int] = None
    recursive: bool = False
    ocr_pages: int = 5
    hash_skip_dirs: Set[Path] = field(default_factory=set)
    run_stamp: str = ""
    print_prompts: bool = False
    prompts_only: bool = False
    prompts_path: Optional[Path] = None


@dataclass
class FileOutcome:
    src: Path
    dest: Optional[Path]
    action: str
    reason: str = ""
    classification: Dict[str, object] = field(default_factory=dict)
    provider: str = ""
    elapsed_s: float = 0.0


@dataclass
class BatchResult:
    outcomes: List[FileOutcome] = field(default_factory=list)
    skipped_duplicates: int = 0

    def tally(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for o in self.outcomes:
            counts[o.action] = counts.get(o.action, 0) + 1
        counts["skip-duplicate"] = self.skipped_duplicates
        return counts


def _ocr_or_empty(path: Path, max_pages: int) -> str:
    try:
        return ocr_mod.extract_text(path, max_pages=max_pages)
    except ocr_mod.OCRUnavailable as exc:
        log.error("OCR toolchain unavailable: %s", exc)
        return ""


class PromptDumpWriter:
    """Append-only JSONL writer for prompt dumps (mode 0600)."""

    def __init__(self, path: Path):
        self.path = path
        self._fh: Optional[TextIO] = None
        self._fd: Optional[int] = None

    def __enter__(self) -> "PromptDumpWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        self._fd = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(self._fd, 0o600)
        except OSError:
            pass
        self._fh = os.fdopen(self._fd, "w", encoding="utf-8")
        self._fd = None
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh:
            self._fh.flush()
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                pass
            self._fh.close()
            self._fh = None

    def write(self, record: Dict[str, object]) -> None:
        if not self._fh:
            raise RuntimeError("PromptDumpWriter not opened")
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()


def _classify_file(
    text: str,
    file_path: Path,
    taxonomy: Taxonomy,
    settings: ClassifierSettings,
    sensitive_terms: SensitiveTerms,
    print_prompts: bool = False,
    prompts_only: bool = False,
    prompts_writer: Optional[PromptDumpWriter] = None,
) -> Dict[str, object]:
    """Single redaction gate shared by every provider path.

    ``text`` is OCR output. We *always* run it through
    :func:`scanorganizer.privacy.gate` first, which decides whether the
    document is safe to leave the host at all. Medical / injury keywords
    force local-only regardless of category. The original filename and
    path are checked the same way: a private-term hit on either marks the
    file ``local-only: sensitive``. Outbound prompts are built from
    allowlisted signals only — OCR text, filenames, and paths are never
    interpolated. Sensitive documents fall through to the local keyword
    classifier and the manifest records a ``local-only: sensitive`` entry
    together with category counts (never the matched strings themselves).
    """
    report: RedactionReport = gate(text, sensitive_terms)
    # Filenames and parent path segments are not OCR'd, so they must be
    # checked the same way as document text. A private-term hit here is
    # treated as sensitive content: local-only, never a cloud provider.
    name_hits = sensitive_terms.count_hits(file_path.name)
    path_hits = sensitive_terms.count_hits(str(file_path))
    label_hits = max(name_hits, path_hits)
    if label_hits:
        report.sensitive = True
        report.sensitive_reason = "private-term-match"
        report.category_counts["private_term"] = (
            report.category_counts.get("private_term", 0) + label_hits
        )
    from . import local_rules
    local_guess = local_rules.classify(text, taxonomy, file_path.name)
    category_blocked = is_local_only_category(str(local_guess.get("doc_type") or ""))
    if report.sensitive or category_blocked:
        result = local_guess
        result["provider"] = "local-keyword"
        result["privacy"] = (
            "local-only: sensitive" if report.sensitive else "local-only: category"
        )
        result["privacy_blocked"] = True
        result["privacy_report"] = report.summary_for_manifest()
        return result
    dump_prompts = print_prompts or prompts_only
    if dump_prompts:
        if settings.provider == "ollama":
            # Local model sees the full redacted OCR; do not run the
            # outbound token allowlist (that gate is for cloud only).
            prompt = classifier_mod._build_local_prompt(taxonomy, report.scrubbed_text)
        else:
            signals = extract_outbound_signals(text)
            prompt = classifier_mod._build_prompt(taxonomy, signals)
            assert_safe_for_outbound(prompt, sensitive_terms, taxonomy=taxonomy)
        if prompts_writer is not None:
            prompts_writer.write({
                "src": str(file_path),
                "filename": file_path.name,
                "prompt": prompt,
            })
        if prompts_only:
            result = local_guess
            result["provider"] = "local-keyword"
            result["prompts_only"] = True
            result["privacy_report"] = report.summary_for_manifest()
            return result
    out = classifier_mod.classify(
        text,
        file_path.name,
        taxonomy,
        settings,
        terms=sensitive_terms,
        file_path=str(file_path),
        local_ocr_text=report.scrubbed_text,
    )
    out["privacy_report"] = report.summary_for_manifest()
    return out


def _resolve_node(
    classification: Dict[str, object],
    taxonomy: Taxonomy,
) -> Optional[TaxonomyNode]:
    """Normalize the model path and match it case-insensitively.

    Invalid, empty, or unrecognised paths return ``None`` so the caller
    routes the file to ``_Review/`` and never files it.
    """
    node = taxonomy.match_classification(classification)
    if node is None:
        return None
    classification["owner"] = node.owner
    classification["area"] = node.area
    classification["doc_type"] = node.doc_type
    return node


def _move_or_plan(src: Path, dest: Path, dry_run: bool) -> Path:
    if dry_run:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    final = disambiguate(dest)
    shutil.move(str(src), str(final))
    return final


def _route_to_fallback(
    src: Path,
    target_root: Path,
    original_name: str,
    dry_run: bool,
) -> Path:
    target_root.mkdir(parents=True, exist_ok=True)
    dest = target_root / original_name
    final = disambiguate(dest)
    if not dry_run:
        shutil.move(str(src), str(final))
    return final


def process_file(
    candidate: CandidateFile,
    cfg: RunConfig,
    writer: ManifestWriter,
    known_hashes: Dict[str, Path],
    prompts_writer: Optional[PromptDumpWriter] = None,
) -> FileOutcome:
    t0 = time.perf_counter()

    class _TimedWriter:
        def __init__(self) -> None:
            self.last_elapsed = 0.0

        def write(self, entry: ManifestEntry) -> None:
            self.last_elapsed = time.perf_counter() - t0
            entry.elapsed_s = self.last_elapsed
            writer.write(entry)

    timed = _TimedWriter()
    outcome = _process_file(
        candidate, cfg, timed, known_hashes, prompts_writer,
    )
    outcome.elapsed_s = timed.last_elapsed or (time.perf_counter() - t0)
    log.info(
        "file %s action=%s elapsed=%.3fs",
        candidate.path.name,
        outcome.action,
        outcome.elapsed_s,
    )
    return outcome


def _process_file(
    candidate: CandidateFile,
    cfg: RunConfig,
    writer: ManifestWriter,
    known_hashes: Dict[str, Path],
    prompts_writer: Optional[PromptDumpWriter] = None,
) -> FileOutcome:
    file_path = candidate.path
    try:
        file_hash = sha256_file(file_path)
    except Exception as exc:
        outcome = FileOutcome(src=file_path, dest=None, action="error",
                              reason=f"hash failed: {exc}")
        writer.write(ManifestEntry.new(
            action="error", src=file_path, dest=file_path, sha256="",
            notes=outcome.reason, dry_run=cfg.dry_run,
        ))
        return outcome

    if file_hash in known_hashes:
        existing = known_hashes[file_hash]
        outcome = FileOutcome(
            src=file_path, dest=existing, action="skip-duplicate",
            reason=f"already filed as {existing}",
        )
        writer.write(ManifestEntry.new(
            action="skip-duplicate", src=file_path, dest=existing, sha256=file_hash,
            notes=outcome.reason, dry_run=cfg.dry_run,
        ))
        return outcome

    # docx (or other review-only extensions) go straight to _Review.
    if candidate.review_only or file_path.suffix.lower() in REVIEW_ONLY_EXTS:
        dest = _route_to_fallback(file_path, cfg.review_dir, file_path.name, cfg.dry_run)
        writer.write(ManifestEntry.new(
            action="review", src=file_path, dest=dest, sha256=file_hash,
            notes="unsupported-ext routed to _Review", dry_run=cfg.dry_run,
        ))
        return FileOutcome(src=file_path, dest=dest, action="review",
                           reason="unsupported-ext routed to _Review")

    text = _ocr_or_empty(file_path, cfg.ocr_pages)
    if not text.strip():
        dest = _route_to_fallback(file_path, cfg.unsorted_dir, file_path.name, cfg.dry_run)
        writer.write(ManifestEntry.new(
            action="unsorted", src=file_path, dest=dest, sha256=file_hash,
            notes="empty OCR text", dry_run=cfg.dry_run,
        ))
        return FileOutcome(src=file_path, dest=dest, action="unsorted",
                           reason="empty OCR text")

    try:
        classification = _classify_file(
            text, file_path, cfg.taxonomy, cfg.settings, cfg.sensitive_terms,
            print_prompts=bool(cfg.print_prompts),
            prompts_only=bool(cfg.prompts_only),
            prompts_writer=prompts_writer,
        )
    except Exception as exc:
        log.exception("Classifier threw unexpectedly for %s", file_path)
        dest = _route_to_fallback(file_path, cfg.review_dir, file_path.name, cfg.dry_run)
        writer.write(ManifestEntry.new(
            action="review", src=file_path, dest=dest, sha256=file_hash,
            notes=f"classifier crash: {exc}", dry_run=cfg.dry_run,
        ))
        return FileOutcome(src=file_path, dest=dest, action="review",
                           reason=f"classifier crash: {exc}")

    confidence = float(classification.get("confidence", 0.0) or 0.0)
    provider = str(classification.get("provider") or cfg.settings.provider)
    node = _resolve_node(classification, cfg.taxonomy)

    if classification.get("cloud_cap"):
        dest = _route_to_fallback(file_path, cfg.review_dir, file_path.name, cfg.dry_run)
        reason = "cloud-call-cap"
        writer.write(ManifestEntry.new(
            action="review", src=file_path, dest=dest, sha256=file_hash,
            classification=classification, provider=provider,
            notes=reason, dry_run=cfg.dry_run,
        ))
        return FileOutcome(src=file_path, dest=dest, action="review",
                           reason=reason, classification=classification, provider=provider)

    if node is None or confidence < cfg.taxonomy.confidence_threshold:
        reason = (
            "invalid taxonomy node" if node is None
            else f"confidence {confidence:.2f} < threshold {cfg.taxonomy.confidence_threshold:.2f}"
        )
        dest = _route_to_fallback(file_path, cfg.review_dir, file_path.name, cfg.dry_run)
        writer.write(ManifestEntry.new(
            action="review", src=file_path, dest=dest, sha256=file_hash,
            classification=classification, provider=provider,
            notes=reason, dry_run=cfg.dry_run,
        ))
        return FileOutcome(src=file_path, dest=dest, action="review",
                           reason=reason, classification=classification, provider=provider)

    doc_date = resolve_doc_date(
        text=text,
        file_path=file_path,
        hinted=str(classification.get("doc_date") or "") or None,
    )
    short_name = sanitize_short_name(str(classification.get("short_name") or ""))
    filename = build_filename(short_name, doc_date, file_path.suffix)
    dest = build_dest_path(cfg.dest, node, filename)
    final_dest = _move_or_plan(file_path, dest, cfg.dry_run)

    action = "plan" if cfg.dry_run else "move"
    writer.write(ManifestEntry.new(
        action=action, src=file_path, dest=final_dest, sha256=file_hash,
        classification={**classification, "resolved_date": doc_date.isoformat()},
        provider=provider, dry_run=cfg.dry_run,
    ))
    if not cfg.dry_run:
        known_hashes[file_hash] = final_dest
    return FileOutcome(src=file_path, dest=final_dest,
                       action=action, classification=classification, provider=provider)


def process_batch(cfg: RunConfig) -> BatchResult:
    cfg.src.mkdir(parents=True, exist_ok=True)
    cfg.dest.mkdir(parents=True, exist_ok=True)
    cfg.review_dir.mkdir(parents=True, exist_ok=True)
    cfg.unsorted_dir.mkdir(parents=True, exist_ok=True)
    cfg.manifest_path.parent.mkdir(parents=True, exist_ok=True)

    known = seen_hashes(cfg.manifest_path.parent)

    skip_dirs = set(cfg.hash_skip_dirs)
    skip_dirs.update({cfg.review_dir, cfg.unsorted_dir, cfg.manifest_path.parent})

    result = BatchResult()
    processed = 0
    prompts_ctx = (
        PromptDumpWriter(cfg.prompts_path)
        if (cfg.print_prompts or cfg.prompts_only) and cfg.prompts_path
        else nullcontext()
    )
    with ManifestWriter(cfg.manifest_path) as writer, prompts_ctx as prompts_writer:
        for candidate in walk_inbox(cfg.src, recursive=cfg.recursive, skip_dirs=skip_dirs):
            if cfg.limit and processed >= cfg.limit:
                break
            try:
                outcome = process_file(
                    candidate, cfg, writer, known, prompts_writer=prompts_writer,
                )
            except Exception as exc:
                log.exception("Unhandled error on %s", candidate.path)
                outcome = FileOutcome(
                    src=candidate.path, dest=None, action="error",
                    reason=f"unhandled: {exc}",
                )
                try:
                    writer.write(ManifestEntry.new(
                        action="error", src=candidate.path, dest=candidate.path,
                        sha256="", notes=str(exc), dry_run=cfg.dry_run,
                    ))
                except Exception:
                    pass
            result.outcomes.append(outcome)
            if outcome.action == "skip-duplicate":
                result.skipped_duplicates += 1
            processed += 1
    return result


def refile_folders(
    legacy_roots: Iterable[Path],
    cfg: RunConfig,
) -> BatchResult:
    """Re-classify files already sitting in legacy folders.

    We flatten them into a single stream and run them through the normal
    classify-and-move loop, honoring dry-run.
    """
    cfg.dest.mkdir(parents=True, exist_ok=True)
    cfg.review_dir.mkdir(parents=True, exist_ok=True)
    cfg.unsorted_dir.mkdir(parents=True, exist_ok=True)
    cfg.manifest_path.parent.mkdir(parents=True, exist_ok=True)

    known = seen_hashes(cfg.manifest_path.parent)

    skip_dirs = set(cfg.hash_skip_dirs)
    skip_dirs.update({cfg.review_dir, cfg.unsorted_dir, cfg.manifest_path.parent})

    result = BatchResult()
    processed = 0
    prompts_ctx = (
        PromptDumpWriter(cfg.prompts_path)
        if (cfg.print_prompts or cfg.prompts_only) and cfg.prompts_path
        else nullcontext()
    )
    with ManifestWriter(cfg.manifest_path) as writer, prompts_ctx as prompts_writer:
        for root in legacy_roots:
            root = Path(root)
            if not root.exists():
                log.warning("Refile root does not exist: %s", root)
                continue
            for candidate in walk_inbox(root, recursive=True, skip_dirs=skip_dirs):
                if cfg.limit and processed >= cfg.limit:
                    break
                try:
                    outcome = process_file(
                        candidate, cfg, writer, known, prompts_writer=prompts_writer,
                    )
                except Exception as exc:
                    log.exception("Unhandled error during refile on %s", candidate.path)
                    outcome = FileOutcome(
                        src=candidate.path, dest=None, action="error",
                        reason=f"unhandled: {exc}",
                    )
                    try:
                        writer.write(ManifestEntry.new(
                            action="error", src=candidate.path, dest=candidate.path,
                            sha256="", notes=str(exc), dry_run=cfg.dry_run,
                        ))
                    except Exception:
                        pass
                result.outcomes.append(outcome)
                if outcome.action == "skip-duplicate":
                    result.skipped_duplicates += 1
                processed += 1
            if cfg.limit and processed >= cfg.limit:
                break
    return result
