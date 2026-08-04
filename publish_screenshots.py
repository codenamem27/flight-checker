"""
Stage flex-grid screenshots for GitHub Pages (option 1: latest-only, self-cleaning).

Copies the current run's screenshots from a source directory into a site
directory under stable, timestamp-free names, and writes an index.html listing
them. The site is meant to be published with actions/deploy-pages, which replaces
the entire site each run — so old screenshots disappear automatically (no prune,
no git-history bloat).

Stable naming and newest-per-grid selection are shared with momondo_flights.py so
the inline <img> URLs in the email match the published files exactly.

Usage:
    python publish_screenshots.py [--source .] [--out _site]
"""

import argparse
import shutil
from datetime import datetime
from pathlib import Path

from momondo_flights import SCREENSHOT_RE, select_screenshots, stable_screenshot_name


def build_site(source: Path, out: Path) -> list[Path]:
    """Copy newest-per-grid screenshots from source into out/screenshots under
    stable names and write out/index.html. Returns the published file paths."""
    shots_dir = out / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    # routes=empty -> include every grid found (a fresh CI checkout only has this
    # run's screenshots), still deduped to the newest file per grid.
    published: list[Path] = []
    rows = []
    for src in select_screenshots(source, routes=set()):
        stable = stable_screenshot_name(src)
        dest_path = shots_dir / stable
        shutil.copyfile(src, dest_path)
        published.append(dest_path)
        m = SCREENSHOT_RE.match(src.name)
        route = m.group(1) if m else stable
        rows.append(
            f'    <figure style="margin:0 0 24px;">'
            f'<figcaption style="font-weight:600;margin-bottom:6px;">{route}</figcaption>'
            f'<a href="screenshots/{stable}"><img src="screenshots/{stable}" '
            f'alt="{route} flexible-dates grid" style="max-width:1024px;width:100%;'
            f'border:1px solid #e2e8f0;border-radius:8px;"></a></figure>'
        )

    generated = datetime.now().strftime("%d %b %Y, %H:%M")
    body = "\n".join(rows) if rows else "    <p>No screenshots for this run.</p>"
    (out / "index.html").write_text(
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"UTF-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Flex-grid screenshots</title></head>"
        "<body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
        "Roboto,sans-serif;max-width:1080px;margin:0 auto;padding:24px;color:#1a202c;\">"
        f"<h1 style=\"font-size:1.3rem;\">Flexible-dates price grids</h1>"
        f"<p style=\"color:#718096;font-size:.85rem;\">Generated {generated}</p>\n"
        f"{body}\n</body></html>\n",
        encoding="utf-8",
    )
    return published


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage flex-grid screenshots for GitHub Pages.")
    parser.add_argument("--source", default=".", help="Directory containing flight_flex_grid_*.png (default: .)")
    parser.add_argument("--out", default="_site", help="Output site directory to publish (default: _site)")
    args = parser.parse_args()

    published = build_site(Path(args.source), Path(args.out))
    print(f"Published {len(published)} screenshot(s) to {args.out}/screenshots/ + index.html")
    for p in published:
        print(f"  {p.name}")


if __name__ == "__main__":
    main()
