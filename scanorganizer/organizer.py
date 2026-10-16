"""Batch orchestration: process_file, process_batch, refile_folder."""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from . import classifier as classifier_mod
from . import ocr as ocr_mod
from .classifier import ClassifierSettings
from .config import REVIEW_ONLY_EXTS
from .dates import resolve_doc_date
from .hashing import sha256_file
from .manifest import ManifestEntry, ManifestWriter, seen_hashes
from .naming import build_dest_path, build_filename, disambiguate, sanitize_short_name
from .privacy import PrivacyDecision, SensitiveTerms, evaluate
from .scanner import CandidateFile, walk_inbox
from .taxonomy import Taxonomy, TaxonomyError, TaxonomyNode

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


@dataclass
class FileOutcome:
    src: Path
    dest: Optional[Path]
    action: str
    reason: str = ""
    classification: Dict[str, object] = field(default_factory=dict)
    provider: str = ""


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


def _classify_file(
    text: str,
    file_path: Path,
    taxonomy: Taxonomy,
    settings: ClassifierSettings,
    sensitive_terms: SensitiveTerms,
) -> Dict[str, object]:
    decision: PrivacyDecision = evaluate(text, sensitive_terms)
    if not decision.outbound_ok:
        from . import local_rules
        result = local_rules.classify(text, taxonomy, file_path.name)
        result["provider"] = "local-keyword"
        result["privacy_blocked"] = True
        result["matched_terms"] = list(decision.matched_terms)
        return result
    scrubbed = decision.scrubbed_text or ""
    return classifier_mod.classify(scrubbed, file_path.name, taxonomy, settings)


def _resolve_node(
    classification: Dict[str, object],
    taxonomy: Taxonomy,
) -> Optional[TaxonomyNode]:
    owner = str(classification.get("owner") or "")
    area = classification.get("area")
    doc_type = str(classification.get("doc_type") or "")
    if not owner or not doc_type:
        return None
    try:
        return taxonomy.node(owner, area, doc_type)
    except TaxonomyError:
        return None


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
    with ManifestWriter(cfg.manifest_path) as writer:
        for candidate in walk_inbox(cfg.src, recursive=cfg.recursive, skip_dirs=skip_dirs):
            if cfg.limit and processed >= cfg.limit:
                break
            try:
                outcome = process_file(candidate, cfg, writer, known)
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
    with ManifestWriter(cfg.manifest_path) as writer:
        for root in legacy_roots:
            root = Path(root)
            if not root.exists():
                log.warning("Refile root does not exist: %s", root)
                continue
            for candidate in walk_inbox(root, recursive=True, skip_dirs=skip_dirs):
                if cfg.limit and processed >= cfg.limit:
                    break
                try:
                    outcome = process_file(candidate, cfg, writer, known)
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
