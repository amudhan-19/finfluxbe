# @file    scripts/cleanup_demo_flux_data.py
#
# Standalone script to remove demo/seed flux_data rows.
# IMPORTANT: Run with --dry-run first to review what would be deleted.
#
# Usage:
#   python scripts/cleanup_demo_flux_data.py            # dry-run (safe default)
#   python scripts/cleanup_demo_flux_data.py --dry-run  # explicit dry-run
#   python scripts/cleanup_demo_flux_data.py --confirm  # execute deletes

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db.session import SessionLocal  # noqa: E402
from app.models.models import FluxData   # noqa: E402

SEED_ENTITY_IDS: frozenset[str] = frozenset({
    "ACM-US", "ACM-CA", "BET-US", "ACM-UK", "ACM-DE", "ACM-IN"
})


def cleanup_demo_flux_data(dry_run: bool) -> None:
    db = SessionLocal()
    try:
        rows = (
            db.query(FluxData)
            .filter(FluxData.entity_id.in_(SEED_ENTITY_IDS))
            .all()
        )

        if not rows:
            print("No demo flux_data rows found for seed entity IDs.")
            return

        by_entity: dict[str, int] = {}
        for r in rows:
            by_entity[r.entity_id] = by_entity.get(r.entity_id, 0) + 1

        print(f"Found {len(rows)} demo flux_data rows across {len(by_entity)} entities:")
        for eid, count in sorted(by_entity.items()):
            print(f"  {eid}: {count} rows")

        if dry_run:
            print("\n[DRY RUN] No changes made. Pass --confirm to execute deletes.")
            return

        for r in rows:
            db.delete(r)
        db.commit()
        print(f"\n[DELETED] {len(rows)} rows removed from seed entities.")

    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove demo/seed flux_data rows for known seed entity IDs."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Show what would be deleted without making changes (default behaviour).",
    )
    group.add_argument(
        "--confirm",
        dest="confirm",
        action="store_true",
        help="Execute the deletion. Cannot be combined with --dry-run.",
    )
    args = parser.parse_args()
    cleanup_demo_flux_data(dry_run=not args.confirm)


if __name__ == "__main__":
    main()
