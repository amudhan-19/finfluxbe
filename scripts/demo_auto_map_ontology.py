#!/usr/bin/env python3
# @file    scripts/demo_auto_map_ontology.py
#
# DISABLED — demo auto-map is no longer supported. Use manual mapping in
# the Admin Ontology UI instead.

from __future__ import annotations

import sys


def main() -> int:
    print(
        "Demo auto-map is disabled. Open Admin → Ontology Mapping, select "
        "GL accounts manually for each caption, Save, then Generate Flux from BC.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
