"""Weekly summary writer."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Iterable, List

from .manifest import ManifestEntry, read_entries


def summarize(entries: Iterable[ManifestEntry]) -> dict:
    counts: Counter[str] = Counter()
    folder_counts: Counter[str] = Counter()
    review: List[str] = []
    unsorted_: List[str] = []
    errors: List[str] = []
    providers: Counter[str] = Counter()

    for entry in entries:
        if entry.dry_run:
            continue
        counts[entry.action] += 1
        if entry.action in {"move", "review", "unsorted"}:
            dest_path = Path(entry.dest)
            folder = str(dest_path.parent)
            folder_counts[folder] += 1
            if entry.provider:
                providers[entry.provider] += 1
            if entry.action == "review":
                review.append(entry.dest)
            if entry.action == "unsorted":
                unsorted_.append(entry.dest)
        if entry.action == "error":
            errors.append(entry.notes or entry.src)
    return {
        "counts": counts,
        "folder_counts": folder_counts,
        "review": review,
        "unsorted": unsorted_,
        "errors": errors,
        "providers": providers,
    }


def write_summary(summary: dict, out_path: Path, when: date | None = None) -> Path:
    when = when or date.today()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    lines.append(f"# scan-organizer summary — {when.isoformat()}")
    lines.append("")

    counts = summary.get("counts", Counter())
    total_moved = counts.get("move", 0) + counts.get("review", 0) + counts.get("unsorted", 0)
    lines.append("## Totals")
    lines.append("")
    lines.append(f"- Files filed into taxonomy: **{counts.get('move', 0)}**")
    lines.append(f"- Files routed to _Review/: **{counts.get('review', 0)}**")
    lines.append(f"- Files routed to _Unsorted/: **{counts.get('unsorted', 0)}**")
    lines.append(f"- Duplicates skipped: **{counts.get('skip-duplicate', 0)}**")
    lines.append(f"- Errors: **{counts.get('error', 0)}**")
    lines.append(f"- Grand total moves this period: **{total_moved}**")
    lines.append("")

    providers = summary.get("providers", Counter())
    if providers:
        lines.append("## Classifier providers")
        lines.append("")
        for prov, n in providers.most_common():
            lines.append(f"- `{prov}`: {n}")
        lines.append("")

    folder_counts = summary.get("folder_counts", Counter())
    if folder_counts:
        lines.append("## Counts per destination folder")
        lines.append("")
        for folder, n in sorted(folder_counts.items()):
            lines.append(f"- `{folder}` — {n}")
        lines.append("")

    review = summary.get("review", [])
    lines.append(f"## _Review list ({len(review)})")
    lines.append("")
    if not review:
        lines.append("_No files needed human review this period._")
    else:
        for item in review:
            lines.append(f"- `{item}`")
    lines.append("")

    unsorted_ = summary.get("unsorted", [])
    if unsorted_:
        lines.append(f"## _Unsorted list ({len(unsorted_)})")
        lines.append("")
        for item in unsorted_:
            lines.append(f"- `{item}`")
        lines.append("")

    errors = summary.get("errors", [])
    if errors:
        lines.append(f"## Errors ({len(errors)})")
        lines.append("")
        for err in errors:
            lines.append(f"- {err}")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def summarize_manifest_files(manifest_paths: Iterable[Path]) -> dict:
    all_entries: List[ManifestEntry] = []
    for p in manifest_paths:
        all_entries.extend(read_entries(p))
    return summarize(all_entries)
