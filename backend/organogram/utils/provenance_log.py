"""
Provenance log — audit trail for every AuthoritativeLeader extraction.

Every record emitted by Agent 2 carries a ProvenanceEntry that tells
analysts exactly: which URL it came from, which source type, which raw
text chunk contained the name, and whether the name was verified to
literally appear in the source HTML (anti-hallucination).

The log is written to JSONL (one record per line) so it can be streamed
and appended without loading the whole file into memory.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class ProvenanceEntry:
    """Single extraction audit record."""
    name: str
    title: str
    source_url: str
    source_type: str                   # firm_website | sec_filing | annual_report
                                       # linkedin_provided
    raw_evidence: str                  # text chunk that contained the name
    verification_status: str           # verified | unverified | skipped
    verification_detail: str           # why verified/unverified
    llm_call_id: Optional[str]         # UUID of the Claude API call, if any
    fetch_cache_hit: bool              # True if HTML came from local cache
    extracted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    firm: str = ""
    engagement_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class ProvenanceLog:
    """
    Append-only JSONL provenance log.
    Written to {output_dir}/agent2_provenance.jsonl
    """

    def __init__(self, output_dir: str | Path):
        self.path = Path(output_dir) / "agent2_provenance.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: list[ProvenanceEntry] = []

    def record(self, entry: ProvenanceEntry):
        self._entries.append(entry)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def entries(self) -> list[ProvenanceEntry]:
        return list(self._entries)

    def summary(self) -> dict:
        total = len(self._entries)
        verified = sum(1 for e in self._entries if e.verification_status == "verified")
        by_source = {}
        for e in self._entries:
            by_source.setdefault(e.source_type, 0)
            by_source[e.source_type] += 1
        return {
            "total_extracted": total,
            "verified": verified,
            "unverified": total - verified,
            "by_source_type": by_source,
        }

    def print_summary(self):
        s = self.summary()
        print(f"[provenance] {s['total_extracted']} leaders extracted | "
              f"{s['verified']} verified | "
              f"{s['unverified']} unverified")
        for src, n in s["by_source_type"].items():
            print(f"  {src:20s}: {n}")
