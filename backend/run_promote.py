"""
run_promote.py — CLI for the §13 nightly promotion batch job.

Reads corrections_ledger.jsonl, promotes qualifying patterns into
region_overlay.csv, and bumps archetype versions.

Usage
-----
    python3 run_promote.py                           # defaults
    python3 run_promote.py --threshold 5 --dry-run  # preview with lower threshold
    python3 run_promote.py --ledger path/to/ledger.jsonl --rules path/to/rules/

Options
-------
  --ledger     Path to the corrections ledger JSONL (default: output/corrections_ledger.jsonl)
  --rules      Path to the rules/ directory      (default: rules/)
  --threshold  Min corrections to trigger promotion (default: 20)
  --dry-run    Print the report without writing any files
  --verbose    Enable DEBUG logging
"""
from __future__ import annotations
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from organogram.utils.ledger_promoter import LedgerPromoter


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Promote recurring analyst corrections into overlay rules."
    )
    parser.add_argument(
        "--ledger",
        default="output/corrections_ledger.jsonl",
        help="Path to corrections_ledger.jsonl (default: output/corrections_ledger.jsonl)",
    )
    parser.add_argument(
        "--rules",
        default="rules",
        help="Path to rules/ directory (default: rules/)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=20,
        help="Min corrections per pattern to trigger promotion (default: 20)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print the report without writing any files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    ledger_path = Path(args.ledger)
    rules_dir   = Path(args.rules)

    if not ledger_path.exists():
        print(f"[run_promote] Ledger not found: {ledger_path}")
        print("  No corrections recorded yet. Nothing to promote.")
        sys.exit(0)

    if not rules_dir.exists():
        print(f"[run_promote] Rules directory not found: {rules_dir}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  Corrections Ledger Promoter")
    print(f"  Ledger   : {ledger_path}")
    print(f"  Rules    : {rules_dir}")
    print(f"  Threshold: {args.threshold}")
    print(f"  Dry run  : {args.dry_run}")
    print(f"{'='*60}\n")

    promoter = LedgerPromoter()
    report = promoter.promote(
        ledger_path=ledger_path,
        rules_dir=rules_dir,
        threshold=args.threshold,
        dry_run=args.dry_run,
    )

    print(report.summary_text())
    print()


if __name__ == "__main__":
    main()
