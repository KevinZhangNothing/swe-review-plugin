#!/usr/bin/env python3
"""Mirror `.claude/skills/*/SKILL.md` into `swe_review/.claude_skills/`.

Why this exists
---------------
`install-skills` resolves its source from the INSTALLED package location:

    swe_review/.claude_skills/          <- first choice (PiAdapter.__init__)
    <repo>/.claude/skills/              <- development fallback

A wheel ships only what is declared as package data, and `.claude/skills/`
lives outside the package. Before this mirror existed, `pip install swe-review`
produced a distribution with no skill sources at all: `install-skills` printed
`{"installed": {}}` and exited 0 (now a hard error).

`.claude/skills/` stays the single editable source of truth (the `SKILL.md`
files are what Claude Code / OpenCode load from a repo checkout). This script
regenerates the packaged copy, so the two can never disagree.

Usage
-----
    python scripts/sync_skill_sources.py           # sync
    python scripts/sync_skill_sources.py --check   # CI: exit 1 if out of sync
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / ".claude" / "skills"
TARGET_DIR = REPO_ROOT / "swe_review" / ".claude_skills"

#: Only the SKILL.md file is packaged. Extra assets (scripts, references) would
#: need their own package-data entry, so they are intentionally not copied —
#: silently dropping them from a wheel is worse than not advertising them.
SKILL_FILE = "SKILL.md"

# NOTE: the packaged copies are byte-identical to the sources — NO banner.
# A SKILL.md must begin with its `---` YAML frontmatter; prepending even an
# HTML comment pushes `name:`/`description:` off the first line and breaks the
# loader that reads them. Provenance is documented here, in README, and by the
# `--check` gate instead of inside the copied file.


def _source_files() -> list[Path]:
    if not SOURCE_DIR.is_dir():
        return []
    return sorted(p / SKILL_FILE for p in SOURCE_DIR.iterdir()
                  if p.is_dir() and (p / SKILL_FILE).is_file())


def _targets(files: list[Path]) -> list[tuple[Path, Path]]:
    return [(f, TARGET_DIR / f.parent.name / SKILL_FILE) for f in files]


def check() -> int:
    """Exit 1 when the packaged mirror is missing or stale."""
    files = _source_files()
    if not files:
        print(f"error: no {SKILL_FILE} under {SOURCE_DIR}", file=sys.stderr)
        return 1

    problems: list[str] = []
    for src, dst in _targets(files):
        if not dst.is_file():
            problems.append(f"missing: {dst.relative_to(REPO_ROOT)}")
        elif not filecmp.cmp(src, dst, shallow=False):
            problems.append(f"stale:   {dst.relative_to(REPO_ROOT)}")

    packaged = {p.parent.name for p in TARGET_DIR.glob(f"*/{SKILL_FILE}")} \
        if TARGET_DIR.is_dir() else set()
    expected = {f.parent.name for f in files}
    for orphan in sorted(packaged - expected):
        problems.append(f"orphan:  {TARGET_DIR.name}/{orphan}/{SKILL_FILE} "
                        f"(no matching {SOURCE_DIR.name}/{orphan})")

    if problems:
        print("skill sources are out of sync:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        print("\nRun: python scripts/sync_skill_sources.py", file=sys.stderr)
        return 1

    print(f"ok: {len(files)} skill sources in sync")
    return 0


def sync() -> int:
    files = _source_files()
    if not files:
        print(f"error: no {SKILL_FILE} under {SOURCE_DIR}", file=sys.stderr)
        return 1

    for src, dst in _targets(files):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # Drop packaged copies whose source disappeared, so the wheel cannot ship
    # a skill the repo no longer defines.
    expected = {f.parent.name for f in files}
    if TARGET_DIR.is_dir():
        for existing in TARGET_DIR.iterdir():
            if existing.is_dir() and existing.name not in expected:
                shutil.rmtree(existing)

    print(f"synced {len(files)} skill sources -> "
          f"{TARGET_DIR.relative_to(REPO_ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="verify the packaged mirror is up to date (CI gate)")
    args = parser.parse_args(argv)
    return check() if args.check else sync()


if __name__ == "__main__":
    sys.exit(main())
