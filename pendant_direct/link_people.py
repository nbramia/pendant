"""Link speaker names to LifeOS people through the CRM people list.

    python -m pendant_direct.link_people [--dry-run] [--data DIR]

For each name in `speakers.allowed` (and the wearer) without a link in `speakers.people`:
The wearer links to LifeOS's own "self" person (`/api/crm/config` my_person_id); a name with
a surname links on a full-name match; a first name links only when one person with that first
name is in circle 2 or closer and at least 1.5x as strong as the next. Anything else is printed
as ambiguous and left unlinked (the app asks). One atomic write to config.json; `--dry-run`
changes nothing. Linked names are never changed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import config, lifeos_people


def run(data_dir: Path, dry_run: bool = False, get=None, out=sys.stdout) -> dict[str, str]:
    """The links chosen (name -> entity_id); written unless dry_run."""
    sp = config.load(data_dir)["speakers"]
    wearer = sp.get("wearer") or None
    names = list(sp.get("allowed", []))
    if wearer and wearer.lower() not in {n.lower() for n in names}:
        names.append(wearer)
    todo = [n for n in names if not config.linked_entity(sp.get("people"), n)]
    chosen: dict[str, str] = {}
    if todo:
        rows, ambiguous = lifeos_people.choose_links(todo, wearer=wearer, get=get)
        for name, row in rows.items():
            chosen[name] = row["entity_id"]
            print(f"{name}: {row['name']} ({row['entity_id']}) circle {row['circle']}, "
                  f"strength {row['strength']}, {row['category']}", file=out)
        for name, cands in ambiguous.items():
            near = "; ".join(f"{c['name']} (circle {c['circle']}, {c['strength']})" for c in cands) or "no one"
            print(f"{name}: ambiguous, not linked (closest: {near})", file=out)
    else:
        print("nothing to link", file=out)
    if chosen and not dry_run:
        config.link_people(data_dir, chosen)
        print(f"linked {len(chosen)}", file=out)
    elif chosen:
        print(f"dry run: would link {len(chosen)}", file=out)
    return chosen


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("PENDANT_DATA", Path.home() / "pendant-direct-data")))
    args = ap.parse_args(argv)
    try:
        run(args.data.expanduser(), args.dry_run)
    except Exception as e:  # LifeOS down, unreadable config
        print(f"link_people failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
