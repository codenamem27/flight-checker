"""
Scrape Momondo "Flexible dates" matrices and report the cheapest date pairs.

Given a flexible-search URL such as:

    https://www.momondo.com.au/flight-search/SYD-MXP/2026-10-18-flexible-2days/2026-10-30-flexible-3days

Momondo renders a "Flexible dates" grid: departure dates on one axis (base date
+/- N days) and return dates on the other. Each cell is a total round-trip
price for that depart/return combination.

This script reads a file of such URLs, scrapes each grid, keeps only date pairs
at least MINDAYS apart (return - depart), and writes the top 3 cheapest pairs
per route to a JSON file.

Usage:
    python momondo_flexible.py [searches.flex.local.txt] [--top 3] [--debug]

Input file: URLs grouped under mandatory section headers of the form
`## DEST,MINDAYS,MAXPRICE` (e.g. `## MXP,9,1400`):
  - DEST     3-letter IATA destination; every URL below must be a search to it
             (validated against the URL's route) or an error is raised.
  - MINDAYS  minimum days between depart and return (return - depart >= MINDAYS).
  - MAXPRICE informational cap: the top 3 cheapest pairs are shown regardless,
             but each is flagged within_cap (price <= MAXPRICE) or not.
Every URL must appear beneath a header, or an "invalid data" error is raised.
Lines starting with a single "#" are comments; other non-URL lines are skipped.

Requires:
    pip install crawl4ai beautifulsoup4
    crawl4ai-setup  # installs Playwright browsers
"""

import argparse
import asyncio
import base64
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

# Route + base dates. The "-flexible-Ndays" suffix on each date is optional so
# this also parses a plain (non-flexible) URL's dates.
ROUTE_RE = re.compile(
    r"/flight-search/([A-Z]{3})-([A-Z]{3})/"
    r"(\d{4}-\d{2}-\d{2})(?:-flexible-\d+days?)?/"
    r"(\d{4}-\d{2}-\d{2})(?:-flexible-\d+days?)?"
)

# Optional passenger segment after the return date, e.g. ".../3adults". Absent
# on single-traveller searches. Carried through to the emitted handoff URLs so
# momondo_flights.py scrapes the same number of travellers as the flex search.
PAX_RE = re.compile(r"/(\d+adults)\b", re.I)

# Each flexible-grid cell: <li id="FlexMatrixCell__{RETURN}_{DEPART}"> where the
# dates are yyyymmdd. Price text lives in a ".jPY1-inner" descendant; its
# modifier class (…-inner-best_price / -bad_price / -default) flags the cell.
CELL_ID_RE = re.compile(r"^FlexMatrixCell__(\d{8})_(\d{8})$")
STATUS_RE = re.compile(r"jPY1-mod-inner-(\w+)")

# Section header: ## DEST,MINDAYS,MAXPRICE  e.g.  ## MXP,9,1400
HEADER_RE = re.compile(r"^##\s*([A-Za-z]{3})\s*,\s*(\d+)\s*,\s*(\d+(?:\.\d+)?)\s*$")

# Sentinel written under a section header when a destination yields no eligible
# date pairs. momondo_flights.py recognises this line and shows a "no dates
# found" note instead of trying to scrape it as a URL.
NO_DATES_TOKEN = "NO_SUITABLE_DATES"


class InvalidDataError(Exception):
    """Raised when the input file has a malformed section header, a URL that
    isn't under any header, or a URL whose destination doesn't match its
    section header."""


def parse_route(url: str) -> tuple[str, str, str, str] | None:
    m = ROUTE_RE.search(url)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def pax_segment(url: str) -> str:
    """Return the URL's passenger path segment with a leading slash (e.g.
    "/3adults"), or "" for a single-traveller URL that has none."""
    m = PAX_RE.search(url)
    return f"/{m.group(1)}" if m else ""


def parse_input_file(path: str) -> list[tuple[str, str, int, float]]:
    """Parse URLs grouped under `## DEST,MINDAYS,MAXPRICE` headers.

    Returns a list of (url, dest, min_days, max_price). Every URL must sit
    beneath a header and its destination must match that header's DEST.
    """
    entries: list[tuple[str, str, int, float]] = []
    dest: str | None = None
    min_days: int | None = None
    max_price: float | None = None
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if line.startswith("##"):
                m = HEADER_RE.match(line)
                if not m:
                    raise InvalidDataError(f"Line {lineno}: malformed section header: {line!r}")
                dest = m.group(1).upper()
                min_days = int(m.group(2))
                max_price = float(m.group(3))
                continue
            if line.startswith("#"):
                continue
            if not line.startswith("http"):
                print(f"Line {lineno}: doesn't look like a URL, skipping: {line!r}")
                continue
            if dest is None:
                raise InvalidDataError(
                    f"Line {lineno}: URL is not under a `## DEST,MINDAYS,MAXPRICE` "
                    f"section header: {line!r}"
                )
            route = parse_route(line)
            url_dest = route[1].upper() if route else "?"
            if route is None or url_dest != dest:
                raise InvalidDataError(
                    f"Line {lineno}: URL destination {url_dest!r} doesn't match "
                    f"section destination {dest!r}: {line!r}"
                )
            entries.append((line, dest, min_days, max_price))
    return entries


def _price_to_float(price: str) -> float | None:
    if not price:
        return None
    cleaned = price.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _yyyymmdd_to_date(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _save_top_half(png_bytes: bytes, path: str) -> None:
    """Save the band of a PNG where the flexible-dates grid sits: from 10% down
    to 50% of the height. The top 10% (nav bar / price charts above the grid) and
    the results list below are dropped. Falls back to the full image if Pillow
    isn't available."""
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        Path(path).write_bytes(png_bytes)
        return
    img = Image.open(BytesIO(png_bytes))
    w, h = img.size
    img.crop((0, int(h * 0.10), w, h // 2)).save(path)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

async def scrape_flex_grid(
    url: str, debug: bool = False, screenshot_path: str | None = None
) -> str | None:
    """Return the raw HTML of the page (containing the flexible-dates grid).

    When screenshot_path is given, a full-page PNG of the rendered grid is
    written there.
    """
    browser_cfg = BrowserConfig(
        headless=True,
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        # momondo results live in an inner scroll container, so crawl4ai's
        # full-page screenshot heuristic collapses to the viewport. Render into
        # a tall viewport instead so the capture reaches the flexible-dates grid
        # that sits below the fold.
        viewport_width=1280,
        viewport_height=2600,
        extra_args=["--disable-blink-features=AutomationControlled"],
    )
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=60_000,
        delay_before_return_html=30.0,
        simulate_user=True,
        magic=True,
        screenshot=True,
        # js_code runs after simulate_user/magic auto-scroll and right before
        # the screenshot, so scroll back to the top here to keep the flexible-
        # dates grid (top of the page) fully in frame. screenshot_wait_for lets
        # the scroll settle before the capture.
        js_code=["window.scrollTo(0, 0);"],
        screenshot_wait_for=0.5,
    )

    print(f"Crawling: {url}\n(This may take 30–60 seconds…)\n")
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        result = await crawler.arun(url=url, config=run_cfg)

    if not result.success:
        print(f"Crawl failed: {result.error_message}")
        return None

    if debug:
        debug_path = "debug_flex.html"
        Path(debug_path).write_text(result.html or "", encoding="utf-8")
        print(f"Raw HTML saved to {debug_path}")

    if screenshot_path:
        if result.screenshot:
            _save_top_half(base64.b64decode(result.screenshot), screenshot_path)
            print(f"Screenshot saved to {screenshot_path}")
        else:
            print("No screenshot captured for this page.")

    return result.html


def parse_flex_grid(html: str) -> list[dict]:
    """Extract every populated flexible-grid cell as a depart/return/price dict."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("BeautifulSoup not installed; cannot parse grid.")
        return []

    soup = BeautifulSoup(html, "html.parser")
    cells: list[dict] = []
    for li in soup.find_all("li", id=CELL_ID_RE):
        m = CELL_ID_RE.match(li.get("id", ""))
        ret_date = _yyyymmdd_to_date(m.group(1))
        dep_date = _yyyymmdd_to_date(m.group(2))

        inner = li.find(class_=re.compile(r"jPY1-inner"))
        if inner is None:
            continue  # cell without a rendered price
        price_str = inner.get_text(strip=True)
        price_val = _price_to_float(price_str)
        if price_val is None:
            continue

        status_m = STATUS_RE.search(" ".join(inner.get("class", [])))
        cells.append({
            "depart_date": dep_date.isoformat(),
            "return_date": ret_date.isoformat(),
            "trip_days": (ret_date - dep_date).days,
            "price": price_str,
            "price_value": price_val,
            "status": status_m.group(1) if status_m else "default",
        })
    return cells


def top_cheapest(cells: list[dict], min_days: int, top_n: int, max_price: float) -> list[dict]:
    eligible = [c for c in cells if c["trip_days"] >= min_days]
    eligible.sort(key=lambda c: c["price_value"])
    top = eligible[:top_n]
    for i, c in enumerate(top, 1):
        c["rank"] = i
        c["within_cap"] = c["price_value"] <= max_price
    return top


# ---------------------------------------------------------------------------
# Handoff: emit a searches.txt-format file for momondo_flights.py
# ---------------------------------------------------------------------------

def build_searches_file(results: list[dict], path: str, section_caps: dict[str, float]) -> int:
    """Write a searches.txt-format file from flex results, for handoff to
    momondo_flights.py.

    One `## DEST,MAXPRICE` section is written per destination in section_caps
    (input order), even when that destination produced no eligible dates — in
    that case a single NO_SUITABLE_DATES sentinel line is written instead of
    URLs. Otherwise, results for the same DEST are merged into the one section
    and one full flight-search URL (with ?sort=price_a, and the source URL's
    passenger segment such as /3adults carried through) is written per unique
    depart/return pair (identical URLs, e.g. an overlapping date pair that
    ranked in two flex grids, are emitted only once).

    Returns the number of real search URLs written (sentinels not counted)."""
    lines = [
        "# Auto-generated by momondo_flexible.py from flex-search results.",
        "# Format matches searches.txt (## DEST,MAXPRICE); consumed by momondo_flights.py.",
    ]
    by_dest: dict[str, list[dict]] = {}
    for r in results:
        by_dest.setdefault(r["destination"], []).append(r)

    n_urls = 0
    for dest, cap in section_caps.items():
        lines.append(f"\n## {dest},{cap:g}")
        seen: set[str] = set()
        emitted = 0
        for r in by_dest.get(dest, []):
            orig = r["origin"]
            p = urlparse(r["url"])
            base = f"{p.scheme}://{p.netloc}"
            # Carry the flex URL's passenger segment (e.g. "/3adults") through so
            # the detail search runs for the same number of travellers.
            pax = pax_segment(r["url"])
            for d in r["top_deals"]:
                url = (
                    f"{base}/flight-search/{orig}-{dest}/"
                    f"{d['depart_date']}/{d['return_date']}{pax}?sort=price_a"
                )
                if url in seen:
                    continue
                seen.add(url)
                lines.append(url)
                emitted += 1
        if emitted == 0:
            lines.append(NO_DATES_TOKEN)
        n_urls += emitted
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return n_urls


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_search(
    url: str,
    index: int,
    total: int,
    min_days: int,
    max_price: float,
    top_n: int,
    debug: bool = False,
    output_dir: Path = Path("."),
) -> dict | None:
    print(f"\n[{index}/{total}] {url}")
    route = parse_route(url)
    if not route:
        print(f"Could not parse route from URL: {url}")
        return None
    orig, dest, dep, ret = route

    label = f"{orig}-{dest}_{dep}_{ret}_flexible"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = str(output_dir / f"flight_flex_grid_{label}_{ts}.png")
    html = await scrape_flex_grid(url, debug=debug, screenshot_path=screenshot_path)
    if not html:
        return None

    cells = parse_flex_grid(html)
    if not cells:
        print(f"No flexible-dates grid found for {orig}-{dest} {dep}/{ret}.")
        return None

    deals = top_cheapest(cells, min_days=min_days, top_n=top_n, max_price=max_price)
    if not deals:
        print(
            f"No date pairs at least {min_days} days apart for "
            f"{orig}-{dest} (scanned {len(cells)} cells)."
        )
        return None

    print(f"\n{'='*72}")
    print(f"  TOP {len(deals)} CHEAPEST  {orig} → {dest}  (≥{min_days} days, cap ${max_price:g}, {len(cells)} cells)")
    print(f"{'='*72}\n")
    for d in deals:
        cap_flag = "≤cap" if d["within_cap"] else "OVER cap"
        print(
            f"#{d['rank']}  {d['price']:<10}  "
            f"Depart {d['depart_date']}  Return {d['return_date']}  "
            f"({d['trip_days']} days)  [{d['status']}]  ({cap_flag})"
        )
    print()

    payload = {
        "route": f"{orig}-{dest}",
        "origin": orig,
        "destination": dest,
        "url": url,
        "depart_base": dep,
        "return_base": ret,
        "min_trip_days": min_days,
        "max_price": max_price,
        "cells_scanned": len(cells),
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "top_deals": deals,
    }
    json_path = f"flight_flex_deals_{label}.json"
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"JSON saved to {json_path}")

    return payload


async def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Momondo flexible-date grids.")
    parser.add_argument(
        "input", nargs="?", default="searches.flex.local.txt",
        help="Input file of flexible-search URLs under `## DEST,MINDAYS,MAXPRICE` headers "
             "(default: searches.flex.local.txt)",
    )
    parser.add_argument("--top", type=int, default=3, help="How many cheapest pairs per route (default: 3)")
    parser.add_argument("--debug", action="store_true", help="Save raw HTML to debug_flex.html")
    parser.add_argument(
        "--emit-searches", metavar="PATH",
        help="After scraping, write a searches.txt-format file (## DEST,MAXPRICE + "
             "top-deal URLs) at PATH for handoff to momondo_flights.py",
    )
    args = parser.parse_args()

    try:
        entries = parse_input_file(args.input)
    except InvalidDataError as e:
        print(f"Invalid data: {e}")
        sys.exit(1)
    if not entries:
        print(f"No URLs found in {args.input}.")
        sys.exit(1)

    total = len(entries)
    print(f"Loaded {total} URL(s) from {args.input}.")

    # Screenshots go in the same directory as the emitted searches file (the
    # build_searches_file() target); without --emit-searches, alongside the
    # per-route JSON files in the CWD.
    output_dir = Path(args.emit_searches).parent if args.emit_searches else Path(".")

    results = []
    for i, (url, _dest, min_days, max_price) in enumerate(entries, 1):
        res = await run_search(
            url, i, total, min_days=min_days, max_price=max_price, top_n=args.top,
            debug=args.debug, output_dir=output_dir,
        )
        if res:
            results.append(res)

    print(f"\nDone. {len(results)}/{total} route(s) produced results.")

    if args.emit_searches:
        # Every destination in the input gets a section, in first-seen order —
        # even ones that produced no eligible dates (marked NO_SUITABLE_DATES).
        section_caps: dict[str, float] = {}
        for _url, dest, _min_days, max_price in entries:
            section_caps.setdefault(dest, max_price)
        n = build_searches_file(results, args.emit_searches, section_caps)
        print(f"Wrote {n} URL(s) across {len(section_caps)} section(s) to {args.emit_searches}")


if __name__ == "__main__":
    asyncio.run(main())
