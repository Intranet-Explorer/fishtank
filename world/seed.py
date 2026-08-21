from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from world.config import REPO_ROOT, Settings

SEED_HABITAT = REPO_ROOT / "seed" / "habitat"
SKIP_COPY_NAMES = {".DS_Store", ".git"}


def _copy_seed(src: Path, dest: Path, *, overwrite: bool) -> int:
    written = 0
    if not src.is_dir():
        raise SystemExit(f"missing seed habitat at {src}")
    dest.mkdir(parents=True, exist_ok=True)
    for path in src.rglob("*"):
        if path.name in SKIP_COPY_NAMES:
            continue
        rel = path.relative_to(src)
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            continue
        shutil.copy2(path, target)
        written += 1
    return written


def seed(habitat: Path | None = None, *, force: bool = False) -> Path:
    settings = Settings()
    habitat = habitat or settings.habitat_root
    n = _copy_seed(SEED_HABITAT, habitat, overwrite=force)
    for agent in ("alpha", "bravo"):
        priv = settings.private_root / agent
        priv.mkdir(parents=True, exist_ok=True)
        state = priv / "STATE.md"
        if not state.exists() or force:
            state.write_text(
                f"# STATE\n\nagent: {agent}\nwakes: 0\nnotes: none yet\n",
                encoding="utf-8",
            )
    print(f"seeded {habitat} ({n} files written, force={force})")
    return habitat


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy the Antfarm seed habitat into habitat/")
    parser.add_argument("--force", action="store_true", help="overwrite seed files in habitat/")
    args = parser.parse_args()
    seed(REPO_ROOT / "habitat", force=args.force)


if __name__ == "__main__":
    main()
