"""
Corrections Ledger — §13 of the Adaptive Organogram Engine master prompt.

Implements an append-only JSONL ledger for analyst overrides.  Every time an
analyst corrects a node's level, function, or reporting line the correction
is appended here.  The `LedgerPromoter` (ledger_promoter.py) consumes this
file nightly to auto-promote recurring patterns into the overlay rule files.

Schema per record (all fields required unless Optional):
  node_id               str      Canonical node id being corrected (e.g. "p_001")
  firm                  str      Firm name from the engagement
  archetype             str      archetype_id (e.g. "industrial_asset_heavy")
  archetype_version     int      Archetype version active at correction time
  region                str      Engine region string (e.g. "Russia", "Africa")
  sub_industry          str|None Sub-industry tag (e.g. "Investment Bank") or None
  original_title_native str      Title as it appeared in source data
  original_title_en     str      English translation / normalized form
  original_level        int      Level before correction (1-10)
  original_function     str      Function before correction
  corrected_level       int      Analyst-specified level
  corrected_function    str      Analyst-specified function
  corrected_reports_to_id str|None  New reports_to_id, or None if unchanged
  correction_reason     str      Free-text analyst note
  analyst_id            str      Who made the correction
  timestamp             str      ISO 8601 UTC  (e.g. "2026-04-30T14:22:01Z")

Usage
-----
    from organogram.utils.corrections_ledger import CorrectionsLedger, CorrectionRecord

    ledger = CorrectionsLedger("output/corrections_ledger.jsonl")
    ledger.append(CorrectionRecord(
        node_id="p_001",
        firm="Acme Automotive Inc.",
        archetype="industrial_asset_heavy",
        archetype_version=1,
        region="Russia",
        sub_industry=None,
        original_title_native="Директор по маркетингу",
        original_title_en="Marketing Director",
        original_level=4,
        original_function="Operations",   # was wrong
        corrected_level=3,
        corrected_function="Marketing",   # corrected
        corrected_reports_to_id=None,
        correction_reason="Russian Marketing Directors report to CMO not COO",
        analyst_id="analyst@refractone.com",
    ))
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CorrectionRecord:
    """One analyst override.  See module docstring for field descriptions."""
    # Node identity
    node_id: str
    firm: str
    archetype: str
    archetype_version: int
    region: str
    sub_industry: Optional[str]

    # What was classified
    original_title_native: str
    original_title_en: str
    original_level: int
    original_function: str

    # What it should be
    corrected_level: int
    corrected_function: str
    corrected_reports_to_id: Optional[str] = None

    # Audit metadata
    correction_reason: str = ""
    analyst_id: str = ""
    timestamp: str = field(default_factory=lambda: _utcnow())

    # ----------------------------------------------------------------
    # Serialisation helpers
    # ----------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CorrectionRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    # Composite key used by the promoter for grouping
    @property
    def composite_key(self) -> tuple:
        return (
            self.archetype,
            self.region,
            (self.sub_industry or "").strip(),
            self.original_title_native.strip().lower(),
        )


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CorrectionsLedger:
    """
    Append-only JSONL ledger of analyst overrides.

    Thread-safe for single-process use (file opened and closed per write).
    For multi-process safety, mount the ledger file on a network filesystem
    or add an external mutex.
    """

    def __init__(self, ledger_path: str | Path):
        self.path = Path(ledger_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # WRITE
    # ------------------------------------------------------------------
    def append(self, record: CorrectionRecord) -> None:
        """
        Append a single correction to the ledger.
        Creates the file if it does not exist (append mode).
        """
        line = json.dumps(record.to_dict(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        logger.debug(
            f"[ledger] appended correction for node={record.node_id!r} "
            f"title={record.original_title_native!r} "
            f"L{record.original_level}/{record.original_function} → "
            f"L{record.corrected_level}/{record.corrected_function}"
        )

    # ------------------------------------------------------------------
    # READ
    # ------------------------------------------------------------------
    def read_all(self) -> list[CorrectionRecord]:
        """Return all records in insertion order."""
        if not self.path.exists():
            return []
        records: list[CorrectionRecord] = []
        with self.path.open(encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    records.append(CorrectionRecord.from_dict(d))
                except (json.JSONDecodeError, TypeError) as exc:
                    logger.warning(f"[ledger] Skipping malformed line {i}: {exc}")
        return records

    # ------------------------------------------------------------------
    # INSPECTION HELPERS
    # ------------------------------------------------------------------
    def count(self) -> int:
        """Number of corrections in the ledger."""
        return len(self.read_all())

    def summary(self) -> dict:
        """
        Return a summary dict useful for dashboards and CLI reporting.
        Groups corrections by composite key and shows counts.
        """
        from collections import Counter
        records = self.read_all()
        key_counts: Counter = Counter(r.composite_key for r in records)
        by_archetype: Counter = Counter(r.archetype for r in records)
        by_region: Counter = Counter(r.region for r in records)

        return {
            "total_corrections": len(records),
            "unique_title_patterns": len(key_counts),
            "by_archetype": dict(by_archetype.most_common()),
            "by_region": dict(by_region.most_common()),
            "top_patterns": [
                {
                    "archetype": k[0], "region": k[1],
                    "sub_industry": k[2] or None, "title": k[3],
                    "count": v,
                }
                for k, v in key_counts.most_common(10)
            ],
        }
