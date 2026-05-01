"""
Ledger Promoter — §13 nightly batch job.

Reads corrections_ledger.jsonl, groups by composite key
(archetype, region, sub_industry, title_native), and promotes
patterns that have hit the consensus threshold into:

  1. rules/region_overlay.csv  — a new overlay row is appended.
  2. rules/archetypes/{id}_v{n}.json — the version field is bumped.
  3. rules/archetypes/_index.json — archetype version updated.

Promotion criteria (per §13):
  - count(key) >= threshold  (default N=20)
  - ALL corrections for that key agree on the same
    (corrected_level, corrected_function)

If corrections disagree, the pattern is flagged as "no_consensus"
and surfaced to analysts for manual review.

Usage
-----
    from organogram.utils.ledger_promoter import LedgerPromoter

    report = LedgerPromoter().promote(
        ledger_path="output/corrections_ledger.jsonl",
        rules_dir="rules",
        threshold=20,
        dry_run=False,
    )
    print(report.summary_text())

CLI:
    python3 run_promote.py --ledger output/corrections_ledger.jsonl
                           --rules  rules/
                           [--threshold 20]
                           [--dry-run]
"""
from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from .corrections_ledger import CorrectionsLedger, CorrectionRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class PromotedRule:
    """One rule that was (or would be) promoted."""
    archetype: str
    region: str
    sub_industry: Optional[str]
    title_native: str
    title_en: str
    corrected_level: int
    corrected_function: str
    correction_count: int
    analyst_ids: list[str]


@dataclass
class SkippedPattern:
    """A pattern that reached threshold but had no consensus."""
    archetype: str
    region: str
    sub_industry: Optional[str]
    title_native: str
    correction_count: int
    disagreements: list[dict]   # [{corrected_level, corrected_function, count}, ...]


@dataclass
class PromotionReport:
    total_corrections: int
    eligible_keys: int          # keys with count >= threshold
    promoted: int               # actually written to overlay
    already_in_overlay: int     # rule already existed — skipped
    no_consensus: int           # conflicting corrections — needs analyst review
    promoted_rules: list[PromotedRule] = field(default_factory=list)
    skipped_no_consensus: list[SkippedPattern] = field(default_factory=list)
    dry_run: bool = False

    def summary_text(self) -> str:
        lines = [
            f"{'[DRY RUN] ' if self.dry_run else ''}Promotion complete — {date.today().isoformat()}",
            f"  Total corrections in ledger : {self.total_corrections}",
            f"  Unique patterns ≥ threshold : {self.eligible_keys}",
            f"  Promoted to overlay         : {self.promoted}",
            f"  Already in overlay (skipped): {self.already_in_overlay}",
            f"  No consensus (needs review) : {self.no_consensus}",
        ]
        if self.promoted_rules:
            lines.append("\nPromoted rules:")
            for r in self.promoted_rules:
                sub = f" [{r.sub_industry}]" if r.sub_industry else ""
                lines.append(
                    f"  {r.archetype}/{r.region}{sub}  "
                    f"'{r.title_native}' → L{r.corrected_level} / {r.corrected_function}"
                    f"  ({r.correction_count} corrections)"
                )
        if self.skipped_no_consensus:
            lines.append("\nNo-consensus patterns (analyst review needed):")
            for s in self.skipped_no_consensus:
                sub = f" [{s.sub_industry}]" if s.sub_industry else ""
                lines.append(
                    f"  {s.archetype}/{s.region}{sub}  '{s.title_native}'"
                    f"  ({s.correction_count} corrections, {len(s.disagreements)} outcome variants)"
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Promoter
# ---------------------------------------------------------------------------

class LedgerPromoter:
    """
    Reads the corrections ledger and promotes qualifying patterns.

    All writes are idempotent — running the promoter twice on the same
    ledger produces the same overlay state.
    """

    def promote(
        self,
        ledger_path: str | Path,
        rules_dir: str | Path,
        threshold: int = 20,
        dry_run: bool = False,
    ) -> PromotionReport:
        """
        Main entry point.

        Parameters
        ----------
        ledger_path : Path to corrections_ledger.jsonl.
        rules_dir   : Path to the rules/ directory (contains region_overlay.csv
                      and archetypes/).
        threshold   : Minimum correction count to trigger promotion (default 20).
        dry_run     : If True, compute the report but write nothing to disk.
        """
        ledger = CorrectionsLedger(ledger_path)
        records = ledger.read_all()
        rules_dir = Path(rules_dir)
        overlay_path = rules_dir / "region_overlay.csv"

        report = PromotionReport(
            total_corrections=len(records),
            eligible_keys=0,
            promoted=0,
            already_in_overlay=0,
            no_consensus=0,
            dry_run=dry_run,
        )

        if not records:
            logger.info("[promoter] Ledger is empty — nothing to promote.")
            return report

        # Step 1 — group by composite key
        groups: dict[tuple, list[CorrectionRecord]] = defaultdict(list)
        for r in records:
            groups[r.composite_key].append(r)

        # Step 2 — load existing overlay for dedup check
        existing = self._load_existing_overlay(overlay_path)

        # Step 3 — track which archetypes need version bumping
        archetypes_to_bump: set[str] = set()

        # Step 4 — evaluate each group
        new_rows: list[dict] = []
        for key, group in groups.items():
            if len(group) < threshold:
                continue
            report.eligible_keys += 1

            archetype, region, sub_industry, _title_l = key
            sub_industry = sub_industry or None

            # Check consensus
            outcome_counts: dict[tuple[int, str], list[str]] = defaultdict(list)
            for r in group:
                outcome_counts[(r.corrected_level, r.corrected_function)].append(r.analyst_id)

            if len(outcome_counts) > 1:
                # No consensus
                report.no_consensus += 1
                report.skipped_no_consensus.append(SkippedPattern(
                    archetype=archetype,
                    region=region,
                    sub_industry=sub_industry,
                    title_native=group[0].original_title_native,
                    correction_count=len(group),
                    disagreements=[
                        {"corrected_level": lvl, "corrected_function": fn,
                         "count": len(ids), "analysts": ids}
                        for (lvl, fn), ids in outcome_counts.items()
                    ],
                ))
                logger.warning(
                    f"[promoter] No consensus for "
                    f"'{group[0].original_title_native}' in {archetype}/{region}: "
                    f"{dict(outcome_counts)}"
                )
                continue

            # Unanimous consensus
            (c_level, c_function), analyst_ids = next(iter(outcome_counts.items())), []
            c_level, c_function = next(iter(outcome_counts))
            analyst_ids = list({a for r in group for a in [r.analyst_id]})

            # Representative record for title_en
            rep = group[0]

            # Dedup check against existing overlay
            dedup_key = (
                rep.original_title_native.strip().lower(),
                region,
                archetype,
                (sub_industry or "").strip(),
            )
            if dedup_key in existing:
                report.already_in_overlay += 1
                logger.debug(
                    f"[promoter] Already in overlay: '{rep.original_title_native}' "
                    f"in {archetype}/{region} — skipping."
                )
                continue

            # Build new overlay row
            note = (
                f"Auto-promoted {date.today().isoformat()}; "
                f"{len(group)} corrections; "
                f"analysts: {', '.join(sorted(set(analyst_ids)))}"
            )
            row = {
                "title_raw":        rep.original_title_native,
                "title_native":     rep.original_title_native,
                "region":           region,
                "archetype_id":     archetype,
                "sub_industry":     sub_industry or "",
                "level":            str(c_level),
                "normalized_title": rep.original_title_en or rep.original_title_native,
                "function":         c_function,
                "notes":            note,
            }
            new_rows.append(row)
            archetypes_to_bump.add(archetype)

            rule = PromotedRule(
                archetype=archetype,
                region=region,
                sub_industry=sub_industry,
                title_native=rep.original_title_native,
                title_en=rep.original_title_en or rep.original_title_native,
                corrected_level=c_level,
                corrected_function=c_function,
                correction_count=len(group),
                analyst_ids=sorted(set(analyst_ids)),
            )
            report.promoted_rules.append(rule)
            report.promoted += 1

        # Step 5 — write overlay rows
        if new_rows and not dry_run:
            self._append_overlay_rows(overlay_path, new_rows)
            logger.info(f"[promoter] Appended {len(new_rows)} rows to {overlay_path}")

        # Step 6 — bump archetype versions
        if archetypes_to_bump and not dry_run:
            for archetype_id in archetypes_to_bump:
                self._bump_archetype_version(rules_dir / "archetypes", archetype_id)

        return report

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------

    @staticmethod
    def _load_existing_overlay(overlay_path: Path) -> set[tuple]:
        """Return set of (title_raw_lower, region, archetype_id, sub_industry) tuples."""
        existing: set[tuple] = set()
        if not overlay_path.exists():
            return existing
        with overlay_path.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                title = (row.get("title_raw") or "").strip()
                if not title or title.startswith("#"):
                    continue
                existing.add((
                    title.lower(),
                    (row.get("region") or "").strip(),
                    (row.get("archetype_id") or "").strip(),
                    (row.get("sub_industry") or "").strip(),
                ))
        return existing

    @staticmethod
    def _append_overlay_rows(overlay_path: Path, rows: list[dict]) -> None:
        """Append new rows to region_overlay.csv, creating file if needed."""
        fieldnames = [
            "title_raw", "title_native", "region", "archetype_id",
            "sub_industry", "level", "normalized_title", "function", "notes",
        ]
        # Write header if file is new
        write_header = not overlay_path.exists() or overlay_path.stat().st_size == 0
        with overlay_path.open("a", newline="", encoding="utf-8") as fh:
            # Section comment
            fh.write(
                f"\n# Auto-promoted by LedgerPromoter on {date.today().isoformat()}\n"
            )
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _bump_archetype_version(archetypes_dir: Path, archetype_id: str) -> None:
        """
        Increment the 'version' field inside the archetype JSON file
        and update the version reference in _index.json.
        Writes the same file in-place (does not create a new versioned file).
        """
        # Find the archetype file (e.g. industrial_asset_heavy_v1.json)
        candidates = sorted(archetypes_dir.glob(f"{archetype_id}_v*.json"))
        if not candidates:
            logger.warning(f"[promoter] Archetype file not found for: {archetype_id}")
            return

        arch_path = candidates[-1]   # highest version file
        data = json.loads(arch_path.read_text(encoding="utf-8"))
        old_version = data.get("version", 1)
        new_version = old_version + 1
        data["version"] = new_version
        arch_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(
            f"[promoter] Bumped {arch_path.name}: v{old_version} → v{new_version}"
        )

        # Update _index.json if it exists and tracks versions
        index_path = archetypes_dir / "_index.json"
        if not index_path.exists():
            return
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for entry in index.get("archetypes", []):
            if entry.get("archetype_id") == archetype_id:
                entry["version"] = new_version
                break
        index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"[promoter] Updated _index.json: {archetype_id} → v{new_version}")
