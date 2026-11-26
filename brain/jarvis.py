import datetime as dt
import json
import math
import re
import sqlite3
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple

SPARK_CHARS = "▁▂▃▄▅▆▇█"
BAR_COLORS = ["bright_cyan", "bright_green", "bright_yellow", "bright_magenta", "cyan", "green"]

RSS_FEEDS: Dict[str, List[str]] = {
    "world": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
    "india": [
        "https://feeds.feedburner.com/ndtvnews-top-stories",
        "https://www.thehindu.com/news/national/feeder/default.rss",
    ],
    "markets": [
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    ],
    "tech": [
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://hnrss.org/frontpage",
    ],
}


def init_jarvis_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT NOT NULL DEFAULT 'general',
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            due_at TEXT NOT NULL,
            message TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def add_note(conn: sqlite3.Connection, tag: str, body: str) -> int:
    cur = conn.execute(
        "INSERT INTO notes (tag, body, created_at) VALUES (?, ?, ?)",
        (tag.strip().lower() or "general", body.strip(), dt.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_notes(conn: sqlite3.Connection, tag_filter: str = "") -> List[Tuple[int, str, str, str]]:
    if tag_filter:
        rows = conn.execute(
            "SELECT id, tag, body, created_at FROM notes WHERE LOWER(tag) LIKE ? ORDER BY id DESC LIMIT 25",
            (f"%{tag_filter.strip().lower()}%",),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, tag, body, created_at FROM notes ORDER BY id DESC LIMIT 25"
        ).fetchall()
    return [(int(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in rows]


def add_reminder(conn: sqlite3.Connection, due_str: str, message: str) -> str:
    now = dt.datetime.now()
    cleaned = due_str.strip()
    parsed_dt: dt.datetime | None = None

    # Accept HH:MM (today, or tomorrow if already passed), YYYY-MM-DD HH:MM, or YYYY-MM-DD
    if re.fullmatch(r"\d{1,2}:\d{2}", cleaned):
        hh, mm = map(int, cleaned.split(":"))
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += dt.timedelta(days=1)
        parsed_dt = candidate
    else:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                p = dt.datetime.strptime(cleaned, fmt)
                if fmt == "%Y-%m-%d":
                    p = p.replace(hour=9, minute=0, second=0)
                parsed_dt = p
                break
            except ValueError:
                continue

    final_due = parsed_dt.strftime("%Y-%m-%d %H:%M") if parsed_dt else cleaned
    conn.execute(
        "INSERT INTO reminders (due_at, message, done, created_at) VALUES (?, ?, 0, ?)",
        (final_due, message.strip(), now.isoformat(timespec="seconds")),
    )
    conn.commit()
    return final_due


def get_reminders(conn: sqlite3.Connection, include_done: bool = False) -> List[Tuple[int, str, str, int]]:
    if include_done:
        rows = conn.execute("SELECT id, due_at, message, done FROM reminders ORDER BY due_at ASC, id ASC").fetchall()
    else:
        rows = conn.execute("SELECT id, due_at, message, done FROM reminders WHERE done = 0 ORDER BY due_at ASC, id ASC").fetchall()
    return [(int(r[0]), str(r[1]), str(r[2]), int(r[3])) for r in rows]


def get_due_reminders(conn: sqlite3.Connection) -> List[Tuple[int, str, str]]:
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = conn.execute(
        "SELECT id, due_at, message FROM reminders WHERE done = 0 AND due_at <= ? ORDER BY due_at ASC",
        (now_str,),
    ).fetchall()
    return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def mark_reminder_done(conn: sqlite3.Connection, rem_id: int) -> bool:
    cur = conn.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rem_id,))
    conn.commit()
    return cur.rowcount > 0


def render_bar_chart(values: List[float], labels: List[str] | None = None, title: str = "Signal Chart") -> str:
    if not values:
        return "No numeric values provided."
    max_val = max(values)
    min_val = min(values)
    avg_val = sum(values) / len(values)
    span = max_val - min_val

    spark_chars = []
    for v in values:
        if span == 0:
            idx = 4
        else:
            norm = (v - min_val) / span
            idx = min(len(SPARK_CHARS) - 1, max(0, int(round(norm * (len(SPARK_CHARS) - 1)))))
        spark_chars.append(SPARK_CHARS[idx])
    sparkline = "".join(spark_chars)

    bar_width = 28
    lines = [
        f"[bold bright_cyan]{title}[/]",
        f"[dim]Sparkline:[/] [bright_green]{sparkline}[/]   [dim]min={min_val:,.2f} · avg={avg_val:,.2f} · max={max_val:,.2f}[/]",
        "",
    ]
    denom = max_val if max_val > 0 else 1.0
    for i, val in enumerate(values):
        lbl = labels[i] if labels and i < len(labels) else f"#{i + 1}"
        filled = int(round((max(0.0, val) / denom) * bar_width))
        filled = max(1 if val > 0 else 0, min(bar_width, filled))
        color = BAR_COLORS[i % len(BAR_COLORS)]
        bar = f"[{color}]{'█' * filled}[/][dim]{'░' * (bar_width - filled)}[/]"
        lines.append(f"  [bold white]{lbl:<12}[/] {bar}  [bold {color}]{val:,.2f}[/]")
    return "\n".join(lines)


def render_pie_chart(slices: List[Tuple[str, float]], title: str = "Distribution Breakdown") -> str:
    clean = [(name.strip(), max(0.0, val)) for name, val in slices if val >= 0]
    total = sum(v for _, v in clean)
    if not clean or total <= 0:
        return "Provide positive slice values, e.g. /pie Study=50,Code=30,Gym=20"

    total_blocks = 40
    strip_parts: List[str] = []
    legend_lines: List[str] = []
    used_blocks = 0

    for idx, (name, val) in enumerate(clean):
        pct = (val / total) * 100.0
        blocks = int(round((val / total) * total_blocks))
        if idx == len(clean) - 1:
            blocks = max(0, total_blocks - used_blocks)
        else:
            blocks = min(blocks, total_blocks - used_blocks)
        used_blocks += blocks
        color = BAR_COLORS[idx % len(BAR_COLORS)]
        if blocks > 0:
            strip_parts.append(f"[{color}]{'█' * blocks}[/]")
        legend_lines.append(f"  [{color}]■[/] [bold white]{name:<14}[/] [bold {color}]{pct:6.2f}%[/]  [dim]({val:,.2f} / {total:,.2f})[/]")

    strip = "".join(strip_parts) or "[dim]No blocks[/]"
    return "\n".join([f"[bold bright_cyan]{title}[/]", f"  [{strip}]", "", *legend_lines])


def render_ratio(a: float, b: float) -> str:
    if b == 0:
        return "Cannot divide by zero in ratio (second value is 0)."
    ratio_ab = a / b
    ratio_ba = b / a if a != 0 else float("inf")
    total = a + b
    pct_a = (a / total) * 100.0 if total != 0 else 0.0
    pct_b = (b / total) * 100.0 if total != 0 else 0.0

    approx_str = ""
    if abs(a - round(a)) < 1e-6 and abs(b - round(b)) < 1e-6:
        ia, ib = int(round(a)), int(round(b))
        if ia != 0 and ib != 0:
            g = math.gcd(abs(ia), abs(ib))
            approx_str = f"\n  [bold white]Simplified Integer Ratio:[/] [bright_green]{ia // g} : {ib // g}[/]"

    return (
        f"[bold bright_cyan]Ratio Analysis ({a:g} vs {b:g})[/]\n"
        f"  [bold white]A : B Decimal:[/] [bright_yellow]{ratio_ab:.4f}[/]  [dim](A = {ratio_ab:.4f} × B)[/]\n"
        f"  [bold white]B : A Decimal:[/] [cyan]{ratio_ba:.4f}[/]{approx_str}\n"
        f"  [bold white]Share Split:[/]   A = [bright_green]{pct_a:.2f}%[/]  |  B = [bright_magenta]{pct_b:.2f}%[/]  [dim](sum = {total:g})[/]"
    )


CURRENCY_ALIASES = {
    "DOLLAR": "USD", "DOLLARS": "USD", "US": "USD", "USD": "USD",
    "RUPEE": "INR", "RUPEES": "INR", "RS": "INR", "INR": "INR",
    "EURO": "EUR", "EUROS": "EUR", "EUR": "EUR",
    "POUND": "GBP", "POUNDS": "GBP", "GBP": "GBP",
    "YEN": "JPY", "JPY": "JPY",
    "WON": "KRW", "KRW": "KRW",
    "YUAN": "CNY", "RMB": "CNY", "CNY": "CNY",
    "AUD": "AUD", "CAD": "CAD", "CHF": "CHF", "SGD": "SGD", "AED": "AED",
}


def parse_fx_args(raw_args: str) -> Tuple[float, str, str]:
    tokens = re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?", raw_args)
    amount = 1.0
    codes: List[str] = []
    for tok in tokens:
        if re.fullmatch(r"\d+(?:\.\d+)?", tok):
            amount = float(tok)
        else:
            mapped = CURRENCY_ALIASES.get(tok.upper())
            if mapped:
                codes.append(mapped)
            elif len(tok) == 3 and tok.isalpha():
                codes.append(tok.upper())
    base = codes[0] if len(codes) >= 1 else "USD"
    target = codes[1] if len(codes) >= 2 else ("INR" if base != "INR" else "USD")
    return amount, base, target


def _fetch_frankfurter(base_u: str, target_u: str, watch: List[str]) -> Tuple[str, Dict[str, float]]:
    quotes_str = ",".join(dict.fromkeys([target_u, *watch]))
    _qs = urllib.parse.urlencode({"base": base_u, "quotes": quotes_str})
    url = f"https://api.frankfurter.dev/v2/rates?{_qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "KARLEXO-CLI/0.7"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("empty frankfurter response")
    rate_date = ""
    rates: Dict[str, float] = {}
    for row in payload:
        q = str(row.get("quote", "")).upper()
        r = float(row.get("rate", 0.0))
        if not rate_date and row.get("date"):
            rate_date = str(row.get("date"))
        if q and r > 0:
            rates[q] = r
    if target_u not in rates:
        raise RuntimeError(f"{target_u} missing in frankfurter")
    return rate_date or "latest", rates


def _fetch_er_api(base_u: str, target_u: str, watch: List[str]) -> Tuple[str, Dict[str, float]]:
    url = f"https://open.er-api.com/v6/latest/{urllib.parse.quote(base_u)}"
    req = urllib.request.Request(url, headers={"User-Agent": "KARLEXO-CLI/0.7"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    if payload.get("result") != "success":
        raise RuntimeError(payload.get("error-type", "er-api error"))
    all_rates = payload.get("rates") or {}
    if target_u not in all_rates:
        raise RuntimeError(f"unsupported currency {target_u}")
    wanted = dict.fromkeys([target_u, *watch])
    rates: Dict[str, float] = {}
    for code in wanted:
        if code in all_rates and float(all_rates[code]) > 0:
            rates[code] = float(all_rates[code])
    date_str = str(payload.get("time_last_update_utc", "latest"))[:16]
    return date_str, rates


def fetch_fx(amount: float, base: str, target: str) -> str:
    base_u = base.strip().upper()
    target_u = target.strip().upper()
    if base_u == target_u:
        return (
            f"[bold bright_cyan]FX Snapshot[/]\n"
            f"  [bold white]{amount:g} {base_u}[/] = [bold bright_green]{amount:,.4f} {target_u}[/]"
        )

    watch = [c for c in ("INR", "USD", "EUR", "GBP", "JPY", "AED", "SGD") if c != base_u]
    source_label = "Frankfurter"
    try:
        rate_date, rate_map = _fetch_frankfurter(base_u, target_u, watch)
    except Exception:
        rate_date, rate_map = _fetch_er_api(base_u, target_u, watch)
        source_label = "ExchangeRate-API"

    main_rate = rate_map[target_u]
    converted = amount * main_rate
    lines = [
        f"[bold bright_cyan]FX Snapshot ({source_label} · {rate_date})[/]",
        f"  [bold white]{amount:g} {base_u}[/] = [bold bright_green]{converted:,.4f} {target_u}[/]  [dim](1 {base_u} = {main_rate:.6g} {target_u})[/]",
        "",
        "[dim]Cross Reference:[/]",
    ]
    for code, r in rate_map.items():
        if code != target_u:
            lines.append(f"  • 1 {base_u} = [cyan]{r:.6g} {code}[/]   [dim]({amount:g} {base_u} -> {amount * r:,.2f} {code})[/]")
    return "\n".join(lines)

def fetch_daily_news(topic: str = "world", limit: int = 8) -> str:
    key = topic.strip().lower() or "world"
    urls = RSS_FEEDS.get(key)
    if not urls:
        q = urllib.parse.quote_plus(topic.strip())
        urls = [f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"]

    headlines: List[Tuple[str, str]] = []
    for feed_url in urls:
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": "KARLEXO-CLI/0.7"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                xml_bytes = resp.read()
            root = ET.fromstring(xml_bytes)
            for item in root.findall(".//item")[:limit]:
                title_el = item.find("title")
                pub_el = item.find("pubDate")
                title = (title_el.text or "").strip() if title_el is not None else ""
                pub = (pub_el.text or "").strip()[:16] if pub_el is not None else ""
                if title:
                    headlines.append((title, pub))
            if len(headlines) >= limit:
                break
        except Exception:
            continue

    if not headlines:
        return f"Could not fetch live headlines for '{topic}' right now (check network or try /news world, /news india, /news markets, /news tech)."

    out = [f"[bold bright_cyan]Daily Briefing — {key.upper()}[/]", ""]
    for idx, (title, pub) in enumerate(headlines[:limit], start=1):
        meta = f" [dim]({pub})[/]" if pub else ""
        out.append(f"  [bold bright_yellow]{idx}.[/] {title}{meta}")
    return "\n".join(out)
