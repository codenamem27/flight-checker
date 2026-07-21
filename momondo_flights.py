"""
Scrape top flight deals from Momondo using Crawl4AI.
Outputs a combined HTML report and per-route JSON files, with optional email delivery.

Usage:
    python momondo_flights.py searches.txt [--email-to you@example.com]

Input file: one Momondo URL per line (lines starting with # are comments).

Carrier filtering: put one carrier name per line in carriers_filter.txt (lines
starting with # are comments) to exclude flights operated by those carriers
from results.

Email environment variables:
    SMTP_HOST  (default: smtp.gmail.com)
    SMTP_PORT  (default: 587)
    SMTP_USER  sender address / login
    SMTP_PASS  password or app password
    EMAIL_FROM (default: SMTP_USER)

Requires:
    pip install crawl4ai beautifulsoup4
    crawl4ai-setup  # installs Playwright browsers
"""

import argparse
import asyncio
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

# Load .env if present (no external dependency)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())


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


def parse_filters_file(path: str) -> set[str]:
    excluded = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            excluded.add(line.lower())
    return excluded


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

async def scrape_top_flights(
    url: str,
    debug: bool = False,
    top_n: int = 5,
    excluded_carriers: set[str] = frozenset(),
) -> list[dict]:
    browser_cfg = BrowserConfig(
        headless=True,
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        extra_args=["--disable-blink-features=AutomationControlled"],
    )
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=60_000,
        delay_before_return_html=30.0,
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

    if debug:
        debug_path = "debug_markdown.md"
        Path(debug_path).write_text(result.markdown or "", encoding="utf-8")
        print(f"Raw markdown saved to {debug_path}")

    flights = _parse_from_markdown(result.markdown, top_n=top_n, excluded_carriers=excluded_carriers)
    if not flights:
        print("Markdown parse yielded no results, trying HTML fallback…")
        flights = _parse_from_html(result.html, top_n=top_n, excluded_carriers=excluded_carriers)

    return flights[:top_n]


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_from_markdown(md: str, top_n: int = 5, excluded_carriers: set[str] = frozenset()) -> list[dict]:
    deals = []
    SEPARATOR   = re.compile(r"Go to (?:next|previous) result")
    LEG_SPLIT   = re.compile(r"^\s{1,4}\d+\.", re.MULTILINE)
    blocks      = SEPARATOR.split(md)

    # Group 1+2: standard linked price [$X,XXX](url)
    # Group 3:   Mix & Match plain-text price  $X,XXX Mix & Match
    price_re   = re.compile(r"\[(\$[\d,]+)\]\((https://[^)]+)\)|(\$[\d,]+)\s+Mix\s*&\s*Match")
    time_re    = re.compile(r"(\d{1,2}:\d{2})\s*[–\-]\s*(\d{1,2}:\d{2}(\+\d)?)")
    airline_re = re.compile(r"!\[([^\]]+)\]\(https://content\.r9cdn")
    seen_prices: set[str] = set()

    for block in blocks:
        if len(deals) >= top_n:
            break
        price_m = price_re.search(block)
        if not price_m:
            continue
        if price_m.group(1):
            price_val, booking_url = price_m.group(1), price_m.group(2)
        else:
            price_val, booking_url = price_m.group(3), ""
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

        if excluded_carriers and any(name in airline.lower() for name in excluded_carriers):
            continue

        deals.append({
            "rank":          len(deals) + 1,
            "airline":       airline,
            "leg1_dep":      l1_dep,
            "leg1_arr":      l1_arr,
            "leg1_duration": _dur(leg1),
            "leg1_stops":    _stops(leg1),
            "leg2_dep":      l2_dep,
            "leg2_arr":      l2_arr,
            "leg2_duration": _dur(leg2),
            "leg2_stops":    _stops(leg2),
            "price":         price_val,
            "booking_link":  booking_url,
        })

    return deals


def _parse_from_html(html: str, top_n: int = 5, excluded_carriers: set[str] = frozenset()) -> list[dict]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("BeautifulSoup not installed; skipping HTML fallback.")
        return []

    soup = BeautifulSoup(html, "html.parser")
    deals = []
    # Match stable semantic suffix; the 4-char prefix (e.g. "Fxw9-") is obfuscated and rotates per deploy
    cards = soup.find_all("div", class_=re.compile(r"-result-item-container", re.I))

    # Stable suffix for price container; 4-char prefix (e.g. "e2GB-") rotates per deploy
    price_container_re = re.compile(r"price-text-container$")

    for card in cards:
        if len(deals) >= top_n:
            break

        text = card.get_text(" ", strip=True)
        price_el   = card.find(class_=price_container_re)
        price_m    = re.search(r"\$[\d,]+", price_el.get_text() if price_el else text)
        time_m     = re.search(r"(\d{1,2}:\d{2})\s*[→–-]\s*(\d{1,2}:\d{2})", text)
        duration_m = re.search(r"(\d+h\s*\d*m?)", text)

        for attr in ("alt", "title", "aria-label"):
            el = card.find(attrs={attr: True})
            if el and el.get(attr):
                airline = el[attr]
                break
        else:
            airline = "Unknown"

        if excluded_carriers and any(name in airline.lower() for name in excluded_carriers):
            continue

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

_CSS = """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f0f4f8; color: #1a202c; padding: 1rem;
    }
    .card {
      background: #fff; border-radius: 12px;
      box-shadow: 0 4px 20px rgba(0,0,0,.08); max-width: 980px;
      margin: 0 auto; overflow: hidden;
    }
    header {
      background: linear-gradient(135deg, #005b99 0%, #0080cc 100%);
      color: #fff; padding: 1.2rem 1.5rem;
    }
    header h1 { font-size: 1.3rem; font-weight: 700; }
    header p  { font-size: .85rem; opacity: .8; margin-top: .3rem; }
    header p.generated { font-size: .75rem; opacity: .6; margin-top: .5rem; }
    section { border-top: 3px solid #e2e8f0; }
    .route-header {
      display: flex; align-items: center; justify-content: space-between;
      flex-wrap: wrap; gap: .4rem;
      padding: .8rem 1rem; background: #edf2f7;
    }
    .route-title { font-weight: 700; font-size: .9rem; color: #2d3748; }
    .route-link  { font-size: .78rem; color: #005b99; text-decoration: none; white-space: nowrap; }
    .route-link:hover { text-decoration: underline; }
    table { width: 100%; border-collapse: collapse; }
    th {
      background: #f7fafc; text-transform: uppercase; font-size: .65rem;
      letter-spacing: .06em; color: #718096; padding: .6rem .75rem;
      text-align: left; border-bottom: 1px solid #e2e8f0;
    }
    td {
      padding: .75rem .75rem; border-bottom: 1px solid #edf2f7;
      font-size: .85rem; vertical-align: middle;
    }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #f7fafc; }
    td.rank    { color: #a0aec0; font-size: .8rem; width: 2rem; }
    td.airline { vertical-align: middle; }
    td.leg-label { font-size: .7rem; color: #718096; white-space: nowrap; }
    tr.leg2 td { border-bottom: 2px solid #e2e8f0; padding-top: .35rem; padding-bottom: .75rem; }
    tr:not(.leg2) td { border-bottom: none; padding-bottom: .35rem; }
    td.price a { font-weight: 700; font-size: 1rem; color: #005b99; text-decoration: none; }
    td.price a:hover { text-decoration: underline; }
    td.time  { font-variant-numeric: tabular-nums; white-space: nowrap; }
    .arrow   { color: #a0aec0; }
    .badge {
      display: inline-block; padding: .15rem .5rem;
      border-radius: 999px; font-size: .72rem; font-weight: 600;
    }
    .badge-nonstop { background: #c6f6d5; color: #276749; }
    .badge-stop    { background: #bee3f8; color: #2a69ac; }
    .meta {
      padding: .75rem 1rem; font-size: .75rem; color: #a0aec0;
      background: #f7fafc; border-top: 1px solid #e2e8f0;
    }

    /* Mobile: collapse table into stacked deal cards */
    @media (max-width: 600px) {
      body { padding: 0; }
      .card { border-radius: 0; box-shadow: none; }
      table, thead, tbody, tr, th, td { display: block; width: 100%; }
      thead { display: none; }
      tr:not(.leg2) { padding: .75rem 1rem .2rem; border-top: 1px solid #e2e8f0; }
      tr.leg2       { padding: .2rem 1rem .75rem; border-bottom: 2px solid #e2e8f0; }
      td { padding: .1rem 0; border: none; font-size: .88rem; }
      td.rank   { display: none; }
      td.price  { font-size: 1.1rem; font-weight: 700; float: right; padding-top: 0; }
      td.price a { font-size: 1.1rem; }
      td.airline { font-weight: 600; padding-bottom: .3rem; max-width: 65%; }
      td.leg-label { display: inline; font-size: .72rem; color: #718096; }
      td.time  { display: inline; margin-left: .3rem; }
      td[data-col="duration"] { display: inline; margin-left: .5rem; color: #718096; font-size: .8rem; }
      td[data-col="stops"]   { display: inline; margin-left: .4rem; }
    }
"""


def parse_route(url: str) -> tuple[str, str, str, str] | None:
    m = ROUTE_RE.search(url)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def _badge(stops: str) -> str:
    if stops == "0":
        return '<span style="display:inline-block;background:#c6f6d5;color:#276749;padding:1px 7px;border-radius:999px;font-size:.72em;font-weight:600;">Non-stop</span>'
    n = int(stops) if stops.isdigit() else 1
    label = f"{stops} stop{'s' if n != 1 else ''}"
    return f'<span style="display:inline-block;background:#bee3f8;color:#2a69ac;padding:1px 7px;border-radius:999px;font-size:.72em;font-weight:600;">{label}</span>'


def _render_route_section(orig: str, dest: str, dep: str, ret: str, url: str, deals: list[dict]) -> str:
    dep_fmt = datetime.strptime(dep, "%Y-%m-%d").strftime("%a %-d/%-m")
    ret_fmt = datetime.strptime(ret, "%Y-%m-%d").strftime("%a %-d/%-m")
    label   = f"{orig} → {dest} &nbsp;|&nbsp; {dep_fmt} – {ret_fmt}"

    cards = ""
    for d in deals:
        price_link = (
            f'<a href="{d["booking_link"]}" target="_blank" '
            f'style="font-size:1.15rem;font-weight:700;color:#005b99;text-decoration:none;">{d["price"]}</a>'
        ) if d["booking_link"] else (
            f'<span style="font-size:1.15rem;font-weight:700;color:#005b99;">{d["price"]}</span>'
        )
        cards += f"""
  <div style="padding:12px 16px;border-bottom:1px solid #e2e8f0;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px;">
      {price_link}
      <span style="font-size:.75rem;color:#a0aec0;">#{d['rank']}</span>
    </div>
    <div style="font-size:.85rem;color:#2d3748;font-weight:600;margin-bottom:7px;">{d['airline']}</div>
    <div style="font-size:.8rem;color:#4a5568;line-height:1.8;">
      ✈ Out &nbsp;{d['leg1_dep']} → {d['leg1_arr']} &nbsp;{d['leg1_duration']} &nbsp;{_badge(d['leg1_stops'])}
    </div>
    <div style="font-size:.8rem;color:#4a5568;line-height:1.8;">
      ✈ Ret &nbsp;{d['leg2_dep']} → {d['leg2_arr']} &nbsp;{d['leg2_duration']} &nbsp;{_badge(d['leg2_stops'])}
    </div>
  </div>"""

    return f"""
  <div style="border-top:3px solid #e2e8f0;">
    <div style="padding:10px 16px;background:#edf2f7;">
      <a href="{url}" target="_blank" style="font-weight:700;font-size:.88rem;color:#2d3748;text-decoration:underline;">{label}</a>
    </div>
    {cards}
  </div>"""


def build_combined_html(all_results: list[tuple]) -> str:
    generated = datetime.now().strftime("%d %b %Y, %H:%M")
    n = len(all_results)
    subtitle = f"{n} route{'s' if n != 1 else ''} · Sorted by price · Scraped from Momondo"
    sections = "".join(
        _render_route_section(orig, dest, dep, ret, url, deals)
        for orig, dest, dep, ret, url, deals in all_results
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Flight Deals</title>
</head>
<body style="margin:0;padding:8px;background:#f0f4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1a202c;">
  <div style="background:#fff;border-radius:12px;max-width:640px;margin:0 auto;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.08);">
    <div style="background:linear-gradient(135deg,#005b99 0%,#0080cc 100%);color:#fff;padding:16px 20px;">
      <div style="font-size:1.2rem;font-weight:700;">Top Flight Deals</div>
      <div style="font-size:.82rem;opacity:.8;margin-top:4px;">{subtitle}</div>
      <div style="font-size:.72rem;opacity:.6;margin-top:6px;">Generated {generated}</div>
    </div>
    {sections}
    <div style="padding:10px 16px;font-size:.75rem;color:#a0aec0;background:#f7fafc;border-top:1px solid #e2e8f0;">Generated {generated}</div>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(html: str, subject: str, to_addr: str) -> None:
    host      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port      = int(os.environ.get("SMTP_PORT", "587"))
    user      = os.environ.get("SMTP_USER", "")
    password  = os.environ.get("SMTP_PASS", "")
    from_addr = os.environ.get("EMAIL_FROM", user)

    if not user or not password:
        print("Email skipped: set SMTP_USER and SMTP_PASS environment variables to enable.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.attach(MIMEText(html, "html"))

    print(f"Sending email to {to_addr} via {host}:{port}…")
    with smtplib.SMTP(host, port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(from_addr, to_addr, msg.as_string())
    print("Email sent.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_search(
    url: str,
    index: int,
    total: int,
    debug: bool = False,
    top_n: int = 5,
    excluded_carriers: set[str] = frozenset(),
) -> tuple | None:
    print(f"\n[{index}/{total}] {url}")
    route = parse_route(url)
    if not route:
        print(f"Could not parse route from URL: {url}")
        return None
    orig, dest, dep, ret = route

    deals = await scrape_top_flights(url, debug=debug, top_n=top_n, excluded_carriers=excluded_carriers)
    if not deals:
        print(f"No deals found for {orig}-{dest} {dep}/{ret}.")
        return None

    label = f"{orig}-{dest}_{dep}_{ret}"
    print(f"\n{'='*60}")
    print(f"  TOP {len(deals)} DEALS  {orig} → {dest}  {dep} / {ret}")
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

    return (orig, dest, dep, ret, url, deals)


async def main():
    parser = argparse.ArgumentParser(description="Scrape Momondo flight deals.")
    parser.add_argument("input_file", help="Text file with one Momondo URL per line")
    parser.add_argument("--email-to", metavar="ADDRESS", default=os.environ.get("EMAIL_TO"), help="Send combined report to this address (or set EMAIL_TO in .env)")
    parser.add_argument("--top-n", type=int, default=int(os.environ.get("TOP_N_RESULTS", 5)), help="Number of top deals to show per route (or set TOP_N_RESULTS env var)")
    parser.add_argument("--debug", action="store_true", help="Save raw markdown from first URL to debug_markdown.md")
    parser.add_argument("--filters-file", default="carriers_filter.txt", help="Text file with one carrier name per line to exclude from results (default: carriers_filter.txt, if present)")
    args = parser.parse_args()

    urls = parse_input_file(args.input_file)
    if not urls:
        print("No valid URLs found in input file.")
        sys.exit(1)

    print(f"Found {len(urls)} URL(s) to run.")

    excluded_carriers: set[str] = set()
    if Path(args.filters_file).exists():
        excluded_carriers = parse_filters_file(args.filters_file)
        print(f"Excluding {len(excluded_carriers)} carrier(s) from {args.filters_file}")

    all_results = []
    for i, url in enumerate(urls, 1):
        result = await run_search(
            url, i, len(urls),
            debug=args.debug and i == 1,
            top_n=args.top_n,
            excluded_carriers=excluded_carriers,
        )
        if result:
            all_results.append(result)
        if i < len(urls):
            await asyncio.sleep(3)

    if not all_results:
        print("No results to report.")
        return

    html = build_combined_html(all_results)
    combined_path = "flight_deals_combined.html"
    Path(combined_path).write_text(html, encoding="utf-8")
    print(f"\nCombined report saved to {combined_path}")
    routes = ", ".join(f"{o}→{d}" for o, d, *_ in all_results)
    subject = f"Flight Deals – {routes}"

    if args.email_to:
        send_email(html, subject, args.email_to)


if __name__ == "__main__":
    asyncio.run(main())
