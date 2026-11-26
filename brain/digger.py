import re
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path


def _clean_text(raw: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", raw)
    text = unescape(no_tags)
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("|", "/")


def _heuristic_metrics(category: str, title: str, summary: str) -> tuple[int, int, int]:
    blob = f"{title} {summary}".lower()
    nov = 7
    imp = 6
    cred = 6

    if any(k in blob for k in ["release", "open-weights", "paper", "benchmark", "breakthrough", "v1", "v2", "cve"]):
        nov = 9
    if any(k in blob for k in ["ai", "llm", "gpu", "semiconductor", "quant", "market", "fed", "rate", "earnings", "math"]):
        imp = 8
    if any(k in blob for k in ["arxiv.org", "github.com", "reuters", "bloomberg", "sec.gov", "official"]):
        cred = 8
    if any(k in blob for k in ["rumor", "telegram", "1000x", "meme", "unverified"]):
        cred = 2

    if category == "MEME_COIN":
        cred = min(cred, 3)

    return nov, imp, cred


def dig_and_score(query: str, scorer_bin: Path, limit: int = 6) -> str:
    """Fetches items from HN/arXiv RSS or a direct URL, scores them via C++20 scorer."""
    items: list[tuple[str, str, str]] = []

    if query.startswith("http://") or query.startswith("https://"):
        try:
            req = urllib.request.Request(query, headers={"User-Agent": "KARLEXO-Digger/0.2"})
            with urllib.request.urlopen(req, timeout=15) as res:
                raw = res.read().decode("utf-8", errors="replace")
            title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
            title = _clean_text(title_match.group(1)) if title_match else query
            body = _clean_text(raw)[:400]
            items.append(("WEB", title[:120], body))
        except Exception as exc:
            return f"Failed to fetch URL '{query}': {exc}"
    else:
        encoded = urllib.parse.quote(query)
        feeds = [
            ("AI", f"https://hnrss.org/newest?q={encoded}"),
            ("RESEARCH", f"https://export.arxiv.org/api/query?search_query=all:{encoded}&start=0&max_results={limit}"),
        ]
        for default_cat, url in feeds:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "KARLEXO-Digger/0.2"})
                with urllib.request.urlopen(req, timeout=12) as res:
                    xml_bytes = res.read()
                root = ET.fromstring(xml_bytes)
                for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry")[:limit]:
                    t_el = entry.find("{http://www.w3.org/2005/Atom}title")
                    s_el = entry.find("{http://www.w3.org/2005/Atom}summary")
                    title = _clean_text(t_el.text if t_el is not None and t_el.text else "Untitled")[:120]
                    summary = _clean_text(s_el.text if s_el is not None and s_el.text else "")[:240]
                    items.append((default_cat, title, summary))
                for item in root.findall(".//item")[:limit]:
                    t_el = item.find("title")
                    d_el = item.find("description")
                    title = _clean_text(t_el.text if t_el is not None and t_el.text else "Untitled")[:120]
                    desc = _clean_text(d_el.text if d_el is not None and d_el.text else "")[:240]
                    items.append((default_cat, title, desc))
            except Exception:
                continue

    if not items:
        return f"No external items fetched for '{query}'."

    scorer_lines = []
    for cat, title, summary in items[: limit * 2]:
        nov, imp, cred = _heuristic_metrics(cat, title, summary)
        scorer_lines.append(f"{cat}|{title}|{nov}|{imp}|{cred}")

    raw_payload = "\n".join(scorer_lines) + "\n"
    if not scorer_bin.exists():
        return "C++ scorer binary missing.\n" + raw_payload

    proc = subprocess.run(
        [str(scorer_bin)],
        input=raw_payload,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()
