#!/usr/bin/env python3
"""Prepare the PTX2RPP static browser app for local hosting or Cloudflare Pages.

Run from anywhere:
    python web/prepare.py
Output:
    web/dist/
"""
from pathlib import Path
import shutil


def main() -> None:
    web = Path(__file__).resolve().parent
    repo = web.parent
    source = repo / "ptx2rpp"
    dist = web / "dist"

    if not (source / "converter.py").is_file():
        raise SystemExit(f"Cannot find {source / 'converter.py'}; place web/ in the PTX2RPP repository.")

    static_files = ("index.html", "main.js", "worker.js", "styles.css")
    missing = [name for name in static_files if not (web / name).is_file()]
    if missing:
        raise SystemExit(f"Missing browser files: {', '.join(missing)}")

    python_files = sorted(source.glob("*.py"))
    if not python_files:
        raise SystemExit(f"No Python package files found in {source}")

    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir(parents=True)
    for name in static_files:
        shutil.copy2(web / name, dist / name)

    package_out = dist / "ptx2rpp"
    package_out.mkdir()
    for path in python_files:
        shutil.copy2(path, package_out / path.name)

    print(f"PTX2RPP Web build prepared: {dist}")
    print(f"Copied {len(static_files)} static files and {len(python_files)} Python files.")


if __name__ == "__main__":
    main()
