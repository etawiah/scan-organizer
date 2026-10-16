"""Command-line interface.

Two modes:

* **Flag mode / --non-interactive**: never calls input(). Suitable for Docker,
  cron, systemd timers, and CI.
* **Interactive mode**: triggered when scan_organizer is invoked with no
  arguments at all. Mirrors the original prompt-driven workflow.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__
from .classifier import ClassifierSettings
from .config import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_LOG_DIRNAME,
    DEFAULT_OCR_PAGES,
    DEFAULT_REVIEW_NAME,
    DEFAULT_SCANS_ROOT,
    DEFAULT_SRC_NAME,
    DEFAULT_UNSORTED_NAME,
    PROVIDERS,
)
from .manifest import undo as undo_manifest
from .organizer import RunConfig, process_batch, refile_folders
from .privacy import SensitiveTerms
from .summary import summarize, write_summary
from .taxonomy import Taxonomy

log = logging.getLogger("scan_organizer")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scan_organizer",
        description="OCR, classify, and file scanned documents into a YAML taxonomy.",
    )
    p.add_argument("--version", action="version", version=f"scan-organizer {__version__}")
    p.add_argument("--scans-root", type=Path, default=None,
                   help="Root of the mounted scans share (default: /scans or $SCANS_ROOT).")
    p.add_argument("--src", type=Path, default=None,
                   help=f"Inbox folder with files to process (default: <scans-root>/'{DEFAULT_SRC_NAME}').")
    p.add_argument("--dest", type=Path, default=None,
                   help="Root under which taxonomy folders will be written (default: <scans-root>).")
    p.add_argument("--review-dir", type=Path, default=None,
                   help=f"Where low-confidence / invalid files land (default: <dest>/{DEFAULT_REVIEW_NAME}).")
    p.add_argument("--unsorted-dir", type=Path, default=None,
                   help=f"Where empty-OCR files land (default: <dest>/{DEFAULT_UNSORTED_NAME}).")
    p.add_argument("--log", type=Path, default=None,
                   help=f"Directory for JSONL manifests and summaries (default: <dest>/{DEFAULT_LOG_DIRNAME}).")
    p.add_argument("--taxonomy", type=Path, default=None,
                   help="Path to taxonomy.yaml (default: ./taxonomy.yaml next to the entry point).")
    p.add_argument("--private-terms", type=Path, default=None,
                   help="Path to private_terms.txt (default: ./private_terms.txt if present).")

    p.add_argument("--provider", choices=PROVIDERS, default=os.environ.get("SCAN_PROVIDER", "local-keyword"),
                   help="Classifier to use (default: $SCAN_PROVIDER or 'local-keyword').")
    p.add_argument("--model", default=os.environ.get("SCAN_MODEL"),
                   help="Model id override for the chosen provider.")
    p.add_argument("--confidence-threshold", type=float, default=None,
                   help=f"Override the YAML's confidence_threshold (default: taxonomy value or {DEFAULT_CONFIDENCE_THRESHOLD}).")
    p.add_argument("--ocr-pages", type=int, default=DEFAULT_OCR_PAGES,
                   help=f"PDF pages to OCR (default: {DEFAULT_OCR_PAGES}).")

    p.add_argument("--dry-run", action="store_true",
                   help="Write the manifest and print the plan without moving anything.")
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N files (useful for incremental backfills).")
    p.add_argument("--non-interactive", action="store_true",
                   help="Never prompt. Required when stdin is closed (cron, Docker, …).")
    p.add_argument("--recursive", action="store_true",
                   help="Descend into subdirectories of --src (default: top-level only).")

    p.add_argument("--refile", type=Path, action="append", default=None, metavar="DIR",
                   help="Re-classify files in a legacy folder into the new taxonomy. Repeatable.")
    p.add_argument("--undo", type=Path, default=None, metavar="MANIFEST",
                   help="Reverse the moves recorded in MANIFEST and exit.")
    p.add_argument("--summary-only", action="store_true",
                   help="Skip processing; just write a summary from existing manifests in --log.")
    p.add_argument("--no-summary", action="store_true",
                   help="Suppress writing the summary-YYYY-MM-DD.md file at the end of a run.")
    p.add_argument("--verbose", "-v", action="count", default=0,
                   help="Increase logging verbosity (-v info, -vv debug).")
    return p


def _resolve_paths(args: argparse.Namespace) -> argparse.Namespace:
    scans_root = args.scans_root
    if scans_root is None:
        env_root = os.environ.get("SCANS_ROOT")
        scans_root = Path(env_root) if env_root else DEFAULT_SCANS_ROOT
    args.scans_root = scans_root

    if args.src is None:
        args.src = scans_root / DEFAULT_SRC_NAME
    if args.dest is None:
        args.dest = scans_root
    if args.review_dir is None:
        args.review_dir = args.dest / DEFAULT_REVIEW_NAME
    if args.unsorted_dir is None:
        args.unsorted_dir = args.dest / DEFAULT_UNSORTED_NAME
    if args.log is None:
        args.log = args.dest / DEFAULT_LOG_DIRNAME
    if args.taxonomy is None:
        here = Path(__file__).resolve().parent.parent
        args.taxonomy = here / "taxonomy.yaml"
    if args.private_terms is None:
        here = Path(__file__).resolve().parent.parent
        candidates = [
            here / "config" / "private_terms.txt",
            here / "private_terms.txt",
        ]
        for candidate in candidates:
            if candidate.exists():
                args.private_terms = candidate
                break
    return args


def _configure_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _build_runconfig(args: argparse.Namespace) -> RunConfig:
    taxonomy = Taxonomy.load(args.taxonomy)
    if args.confidence_threshold is not None:
        taxonomy.confidence_threshold = float(args.confidence_threshold)
    terms = SensitiveTerms.load(args.private_terms)
    settings = ClassifierSettings(provider=args.provider, model=args.model)

    manifest_name = datetime.utcnow().strftime("manifest-%Y%m%dT%H%M%SZ.jsonl")
    manifest_path = args.log / manifest_name

    return RunConfig(
        src=args.src,
        dest=args.dest,
        review_dir=args.review_dir,
        unsorted_dir=args.unsorted_dir,
        manifest_path=manifest_path,
        taxonomy=taxonomy,
        settings=settings,
        sensitive_terms=terms,
        dry_run=bool(args.dry_run),
        limit=args.limit,
        recursive=bool(args.recursive),
        ocr_pages=int(args.ocr_pages),
        hash_skip_dirs={args.dest},
    )


def _print_outcomes(outcomes, dry_run: bool) -> None:
    header = "PLAN" if dry_run else "RESULT"
    print(f"\n{header}: {len(outcomes)} file(s) considered")
    for o in outcomes:
        tag = o.action.upper()
        prov = f" [{o.provider}]" if o.provider else ""
        dest = o.dest or "-"
        reason = f"  // {o.reason}" if o.reason else ""
        print(f"  {tag:<16}{prov} {o.src} -> {dest}{reason}")


def _write_summary(args: argparse.Namespace, outcomes) -> Optional[Path]:
    if args.no_summary:
        return None
    manifests = sorted(args.log.glob("manifest-*.jsonl"))
    from .summary import summarize_manifest_files
    summary = summarize_manifest_files(manifests)
    today = date.today()
    out = args.log / f"summary-{today.isoformat()}.md"
    return write_summary(summary, out, when=today)


def _run_interactive(parser: argparse.ArgumentParser) -> int:
    print("\nscan-organizer interactive mode")
    print("--------------------------------")
    scans_root_default = os.environ.get("SCANS_ROOT") or str(DEFAULT_SCANS_ROOT)
    scans_root = input(f"Scans root [{scans_root_default}]: ").strip() or scans_root_default
    src_default = str(Path(scans_root) / DEFAULT_SRC_NAME)
    src = input(f"Source folder [{src_default}]: ").strip() or src_default
    provider = input("Provider (anthropic/gemini/local-keyword) [local-keyword]: ").strip() or "local-keyword"
    dry = input("Dry run? (y/N): ").strip().lower() in {"y", "yes"}
    argv = [
        "--scans-root", scans_root,
        "--src", src,
        "--provider", provider,
    ]
    if dry:
        argv.append("--dry-run")
    argv.append("--non-interactive")
    return main(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()

    if argv is None and len(sys.argv) == 1 and sys.stdin.isatty():
        return _run_interactive(parser)

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    args = _resolve_paths(args)

    if args.undo is not None:
        result = undo_manifest(args.undo, dry_run=bool(args.dry_run))
        print(json.dumps({
            "undo_manifest": str(args.undo),
            "reversed": result.reversed_moves,
            "skipped": result.skipped,
            "errors": result.errors,
            "dry_run": bool(args.dry_run),
        }, indent=2))
        return 0 if not result.errors else 1

    if args.summary_only:
        args.log.mkdir(parents=True, exist_ok=True)
        from .summary import summarize_manifest_files
        summary = summarize_manifest_files(sorted(args.log.glob("manifest-*.jsonl")))
        out = write_summary(summary, args.log / f"summary-{date.today().isoformat()}.md")
        print(f"Wrote summary {out}")
        return 0

    cfg = _build_runconfig(args)
    print(f"scan-organizer: provider={cfg.settings.provider} dry_run={cfg.dry_run} limit={cfg.limit}")
    print(f"  src        : {cfg.src}")
    print(f"  dest       : {cfg.dest}")
    print(f"  review_dir : {cfg.review_dir}")
    print(f"  unsorted   : {cfg.unsorted_dir}")
    print(f"  manifest   : {cfg.manifest_path}")
    print(f"  taxonomy   : {args.taxonomy}  (threshold={cfg.taxonomy.confidence_threshold})")
    terms_count = len(cfg.sensitive_terms.terms)
    print(
        f"  privacy    : redaction gate=on  terms_loaded={terms_count}"
        + ("" if args.private_terms else "  (no private_terms.txt found)")
    )

    if args.refile:
        result = refile_folders(args.refile, cfg)
    else:
        result = process_batch(cfg)

    _print_outcomes(result.outcomes, cfg.dry_run)
    counts = result.tally()
    print("\nCounts:", json.dumps(counts))

    summary_path = _write_summary(args, result.outcomes)
    if summary_path:
        print(f"Summary: {summary_path}")

    errors = sum(1 for o in result.outcomes if o.action == "error")
    return 1 if errors else 0
