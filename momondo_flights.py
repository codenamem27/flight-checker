"""
Scrape top 5 flight deals from Momondo using Crawl4AI.
Outputs an HTML report and JSON file per search.

Usage:
    python momondo_flights.py searches.txt

Input file format (one search per line):
    ORIGIN DEST DEPARTURE_DATE RETURN_DATE
    e.g.: SYD PVG 2026-10-18 2026-10-23

Requires:
    pip install crawl4ai beautifulsoup4
    crawl4ai-setup  # installs Playwright browsers
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode


def parse_input_file(path: str) -> list[str]:
    urls = []
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith("http"):
                print(f"Line {lineno}: doesn't look like a URL, skipping: {line!r}")
                continue
            urls.append(line)
    return urls


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

async def scrape_top_5_flights(url: str) -> list[dict]:
    """Returns list of flight deals for the given Momondo URL."""
    browser_cfg = BrowserConfig(
        headless=True,
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    )

    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=60_000,
        delay_before_return_html=8.0,
        simulate_user=True,
        magic=True,
    )

    print(f"Crawling: {url}\n(This may take 30–60 seconds…)\n")

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        result = await crawler.arun(url=url, config=run_cfg)

    if not result.success:
        print(f"Crawl failed: {result.error_message}")
        return []

    print(f"Markdown length: {len(result.markdown or '')}")

    flights = _parse_from_markdown(result.markdown)
    if not flights:
        print("Markdown parse yielded no results, trying HTML fallback…")
        flights = _parse_from_html(result.html)

    return flights[:5]


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_from_markdown(md: str) -> list[dict]:
    deals = []
    SEPARATOR   = re.compile(r"Go to (?:next|previous) result")
    LEG_SPLIT   = re.compile(r"^\s{1,4}\d+\.", re.MULTILINE)
    blocks      = SEPARATOR.split(md)

    price_re   = re.compile(r"\[(\$[\d,]+)\]\((https://[^)]+)\)")
    time_re    = re.compile(r"(\d{1,2}:\d{2})\s*[–\-]\s*(\d{1,2}:\d{2}(\+\d)?)")
    airline_re = re.compile(r"!\[([^\]]+)\]\(https://content\.r9cdn")
    seen_prices: set[str] = set()

    for block in blocks:
        if len(deals) >= 5:
            break
        price_m = price_re.search(block)
        if not price_m:
            continue
        price_val, booking_url = price_m.group(1), price_m.group(2)
        if price_val in seen_prices:
            continue
        seen_prices.add(price_val)

        pre = block[:price_m.start()]

        leg_chunks = LEG_SPLIT.split(pre)
        leg1 = leg_chunks[1] if len(leg_chunks) > 1 else pre
        leg2 = leg_chunks[2] if len(leg_chunks) > 2 else ""

        def _times(chunk):
            m = time_re.search(chunk)
            if not m:
                return "N/A", "N/A"
            return m.group(1), m.group(2)

        def _dur(chunk):
            hits = re.findall(r"^(\d+h\s*\d+m)\s*$", chunk, re.MULTILINE)
            return hits[0] if hits else "N/A"

        def _stops(chunk):
            if re.search(r"non.?stop|direct", chunk, re.I):
                return "0"
            m = re.search(r"(\d+)\s*stops?", chunk, re.I)
            return m.group(1) if m else "N/A"

        l1_dep, l1_arr = _times(leg1)
        l2_dep, l2_arr = _times(leg2)

        airlines = airline_re.findall(pre)
        airline  = ", ".join(dict.fromkeys(airlines)) if airlines else "Unknown"

        deals.append({
            "rank":           len(deals) + 1,
            "airline":        airline,
            "leg1_dep":       l1_dep,
            "leg1_arr":       l1_arr,
            "leg1_duration":  _dur(leg1),
            "leg1_stops":     _stops(leg1),
            "leg2_dep":       l2_dep,
            "leg2_arr":       l2_arr,
            "leg2_duration":  _dur(leg2),
            "leg2_stops":     _stops(leg2),
            "price":          price_val,
            "booking_link":   booking_url,
        })

    return deals


def _parse_from_html(html: str) -> list[dict]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("BeautifulSoup not installed; skipping HTML fallback.")
        return []

    soup = BeautifulSoup(html, "html.parser")
    deals = []
    cards = soup.find_all("div", class_=re.compile(r"resultWrapper|result-item", re.I))

    for card in cards[:5]:
        text = card.get_text(" ", strip=True)
        price_m    = re.search(r"(A\$|AU\$)\s?[\d,]+", text)
        time_m     = re.search(r"(\d{1,2}:\d{2})\s*[→–-]\s*(\d{1,2}:\d{2})", text)
        duration_m = re.search(r"(\d+h\s*\d*m?)", text)

        for attr in ("alt", "title", "aria-label"):
            el = card.find(attrs={attr: True})
            if el and el.get(attr):
                airline = el[attr]
                break
        else:
            airline = "Unknown"

        stops = "Non-stop" if re.search(r"non.?stop|direct", text, re.I) else (
            f"{m.group(1)} stop(s)" if (m := re.search(r"(\d+)\s*stop", text, re.I)) else "N/A"
        )

        deals.append({
            "rank":           len(deals) + 1,
            "airline":        airline,
            "departure_time": time_m.group(1) if time_m else "N/A",
            "arrival_time":   time_m.group(2) if time_m else "N/A",
            "duration":       duration_m.group(1) if duration_m else "N/A",
            "stops":          stops,
            "price":          price_m.group(0) if price_m else "N/A",
            "booking_link":   "",
        })

    return deals


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

ROUTE_RE = re.compile(r"/flight-search/([A-Z]{3})-([A-Z]{3})/(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})")


def parse_route(url: str) -> tuple[str, str, str, str] | None:
    m = ROUTE_RE.search(url)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def _route_label(orig: str, dest: str, dep: str, ret: str) -> str:
    dep_fmt = datetime.strptime(dep, "%Y-%m-%d").strftime("%-d %b %Y")
    ret_fmt = datetime.strptime(ret, "%Y-%m-%d").strftime("%-d %b %Y")
    return f"{orig} → {dest} &nbsp;|&nbsp; {dep_fmt} – {ret_fmt}"


def save_html_report(deals: list[dict], url: str, orig: str, dest: str, dep: str, ret: str, out_path: str):
    route = _route_label(orig, dest, dep, ret)
    generated = datetime.now().strftime("%d %b %Y, %H:%M")

    def _badge(stops):
        cls = "badge-nonstop" if stops == "0" else "badge-stop"
        return f'<span class="badge {cls}">{stops}</span>'

    rows = ""
    for d in deals:
        rows += f"""
        <tr>
          <td class="rank" rowspan="2">#{d['rank']}</td>
          <td class="price" rowspan="2"><a href="{d['booking_link']}" target="_blank">{d['price']}</a></td>
          <td class="airline" rowspan="2">{d['airline']}</td>
          <td class="leg-label">✈ Out</td>
          <td class="time">{d['leg1_dep']} <span class="arrow">→</span> {d['leg1_arr']}</td>
          <td>{d['leg1_duration']}</td>
          <td>{_badge(d['leg1_stops'])}</td>
        </tr>
        <tr class="leg2">
          <td class="leg-label">✈ Ret</td>
          <td class="time">{d['leg2_dep']} <span class="arrow">→</span> {d['leg2_arr']}</td>
          <td>{d['leg2_duration']}</td>
          <td>{_badge(d['leg2_stops'])}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Flight Deals – {route}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{mi
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f0f4f8;
      color: #1a202c;
      padding: 2rem;
    }}
    .card {{
      background: #fff;
      border-radius: 12px;
      box-shadow: 0 4px 20px rgba(0,0,0,.08);
      max-width: 960px;
      margin: 0 auto;
      overflow: hidden;
    }}
    header {{
      background: linear-gradient(135deg, #005b99 0%, #0080cc 100%);
      color: #fff;
      padding: 1.5rem 2rem;
    }}
    header h1 {{ font-size: 1.4rem; font-weight: 700; }}
    header p  {{ font-size: .85rem; opacity: .8; margin-top: .3rem; }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th {{
      background: #f7fafc;
      text-transform: uppercase;
      font-size: .7rem;
      letter-spacing: .06em;
      color: #718096;
      padding: .75rem 1rem;
      text-align: left;
      border-bottom: 1px solid #e2e8f0;
    }}
    td {{
      padding: .85rem 1rem;
      border-bottom: 1px solid #edf2f7;
      font-size: .9rem;
      vertical-align: middle;
    }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #f7fafc; }}
    td.rank  {{ color: #a0aec0; font-size: .8rem; width: 2.5rem; vertical-align: middle; }}
    td.airline {{ vertical-align: middle; }}
    td.leg-label {{ font-size: .72rem; color: #718096; white-space: nowrap; }}
    tr.leg2 td {{ border-bottom: 2px solid #e2e8f0; padding-top: .4rem; padding-bottom: .85rem; }}
    tr:not(.leg2) td {{ border-bottom: none; padding-bottom: .4rem; }}
    td.price a {{
      font-weight: 700;
      font-size: 1.05rem;
      color: #005b99;
      text-decoration: none;
    }}
    td.price a:hover {{ text-decoration: underline; }}
    td.time  {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .arrow   {{ color: #a0aec0; }}
    .badge {{
      display: inline-block;
      padding: .2rem .6rem;
      border-radius: 999px;
      font-size: .75rem;
      font-weight: 600;
    }}
    .badge-nonstop {{ background: #c6f6d5; color: #276749; }}
    .badge-stop    {{ background: #bee3f8; color: #2a69ac; }}
    .meta {{
      padding: .75rem 1rem;
      font-size: .75rem;
      color: #a0aec0;
      background: #f7fafc;
      border-top: 1px solid #e2e8f0;
    }}
  </style>
</head>
<body>
  <div class="card">
    <header>
      <h1>Top 5 Flight Deals &nbsp; {route}</h1>
      <p>Sorted by price &nbsp;·&nbsp; Scraped from Momondo</p>
    </header>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Price</th>
          <th>Airline(s)</th>
          <th>Leg</th>
          <th>Times</th>
          <th>Duration</th>
          <th>Stops</th>
        </tr>
      </thead>
      <tbody>{rows}
      </tbody>
    </table>
    <div class="meta">Generated {generated} &nbsp;·&nbsp; <a href="{url}" target="_blank">View on Momondo</a></div>
  </div>
</body>
</html>"""

    Path(out_path).write_text(html, encoding="utf-8")
    print(f"HTML report saved to {out_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_search(url: str, index: int):
    route = parse_route(url)
    if not route:
        print(f"Could not parse route from URL: {url}")
        return
    orig, dest, dep, ret = route

    deals = await scrape_top_5_flights(url)

    if not deals:
        print(f"No flight deals found for {orig}-{dest} {dep}/{ret}.")
        return

    label = f"{orig}-{dest}_{dep}_{ret}"
    print(f"\n{'='*60}")
    print(f"  TOP {len(deals)} FLIGHT DEALS  {orig} → {dest}  {dep} / {ret}")
    print(f"{'='*60}\n")
    for d in deals:
        print(
            f"#{d['rank']}  {d['price']:<10}  {d['airline']:<35}  "
            f"Out: {d['leg1_dep']} → {d['leg1_arr']} ({d['leg1_duration']}, {d['leg1_stops']})  "
            f"Ret: {d['leg2_dep']} → {d['leg2_arr']} ({d['leg2_duration']}, {d['leg2_stops']})"
        )
    print()

    json_path = f"flight_deals_{label}.json"
    with open(json_path, "w") as f:
        json.dump(deals, f, indent=2)
    print(f"JSON saved to {json_path}")

    html_path = f"flight_deals_{label}.html"
    save_html_report(deals, url, orig, dest, dep, ret, html_path)


async def main():
    parser = argparse.ArgumentParser(description="Scrape Momondo flight deals from a search list.")
    parser.add_argument("input_file", help="Text file with searches (one per line: ORIG DEST DEP_DATE RET_DATE)")
    args = parser.parse_args()

    urls = parse_input_file(args.input_file)
    if not urls:
        print("No valid URLs found in input file.")
        sys.exit(1)

    print(f"Found {len(urls)} URL(s) to run.\n")
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}")
        await run_search(url, i)


if __name__ == "__main__":
    asyncio.run(main())
