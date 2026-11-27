import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Tuple
from dotenv import load_dotenv
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "brain"))
load_dotenv(BASE_DIR / ".env")

from digger import dig_and_score
from intent_decoder import decode_local, decode_with_model_fallback
from jarvis import (
    add_note,
    add_reminder,
    fetch_daily_news,
    fetch_fx,
    parse_fx_args,
    get_due_reminders,
    get_reminders,
    init_jarvis_tables,
    list_notes,
    mark_reminder_done,
    render_bar_chart,
    render_pie_chart,
    render_ratio,
)
from router import ModelRouter

MODES = json.loads((BASE_DIR / "config" / "modes.json").read_text())
ROUTING_PATH = BASE_DIR / "config" / "routing.json"
DB_PATH = BASE_DIR / "data" / "memory.db"
HISTORY_FILE = BASE_DIR / "data" / "prompt_history.txt"
EXPORTS_DIR = BASE_DIR / "exports"
SCORER_BIN = BASE_DIR / "engine" / "scorer"
console = Console()


def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT UNIQUE NOT NULL,
            url TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            task TEXT NOT NULL,
            route TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def append_chat(conn: sqlite3.Connection, role: str, task: str, route: str, content: str) -> None:
    conn.execute(
        "INSERT INTO chat_history (role, task, route, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (role, task, route, content, dt.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def load_recent_chat(conn: sqlite3.Connection, limit: int = 16) -> str:
    fetch_n = max(limit * 2, 24)
    rows = conn.execute(
        "SELECT role, task, content FROM chat_history ORDER BY id DESC LIMIT ?",
        (fetch_n,),
    ).fetchall()
    if not rows:
        return ""
    rows.reverse()
    lines = []
    for role, task, content in rows:
        raw = (content or "").strip()
        if not raw or (role == "user" and raw.startswith("/")):
            continue
        clipped = " ".join(raw.split())
        if len(clipped) > 450:
            clipped = clipped[:447] + "..."
        lines.append(f"[{role.upper()}/{task}] {clipped}")
    if len(lines) > limit:
        lines = lines[-limit:]
    return "\n".join(lines)


def load_memory_context(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT key, value FROM memory").fetchall()
    if not rows:
        return "No saved user memory yet."
    return "\n".join(f"- {k}: {v}" for k, v in rows)


def suggest_task_if_general(current_task: str, prompt: str) -> str | None:
    if current_task != "general":
        return None
    low = prompt.lower()
    if any(w in low for w in ("c++", "cpp", "python", "code", "algorithm", "dp ", "graph", "bug", "compile", "pointer", "std::")):
        return "coding"
    if any(w in low for w in ("probability", "expectation", "kelly", "variance", "stochastic", "matrix", "integral", "quant", "ev ")):
        return "quant"
    return None


def clean_terminal_markdown(text: str) -> str:
    text = re.sub(r"\\\[(.*?)\\\]", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"\\\((.*?)\\\)", r"`\1`", text)
    return text.strip()


def sanitize_prompt_input(raw: str) -> str:
    return re.sub(r"^(?:\[KARLEXO/[^\]]+\]\s*>\s*)+", "", raw.strip()).strip()


def rank_signals_with_cpp(raw_lines: str) -> str:
    if not SCORER_BIN.exists():
        return "C++ scorer binary not compiled yet."
    proc = subprocess.run(
        [str(SCORER_BIN)],
        input=raw_lines,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def run_code_snippet(lang: str, code: str) -> Tuple[int, str, float]:
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        if lang in ("cpp", "c++", "cxx"):
            src = tmp_path / "prog.cpp"
            exe = tmp_path / "prog"
            src.write_text(code)
            comp = subprocess.run(
                ["g++", "-std=c++20", "-O2", "-Wall", str(src), "-o", str(exe)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if comp.returncode != 0:
                return comp.returncode, f"[Compile Error]\n{comp.stderr.strip()}", time.perf_counter() - t0
            run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
        elif lang in ("py", "python", "python3"):
            src = tmp_path / "prog.py"
            src.write_text(code)
            run = subprocess.run([sys.executable, str(src)], capture_output=True, text=True, timeout=10)
        else:
            return 1, f"Unsupported language '{lang}'. Use 'cpp' or 'py'.", time.perf_counter() - t0

    out = (run.stdout or "").strip()
    err = (run.stderr or "").strip()
    combined = out if not err else f"{out}\n[stderr]\n{err}".strip()
    return run.returncode, combined or "(no output)", time.perf_counter() - t0


def open_url_in_host(url: str) -> str:
    if not (url.startswith("http://") or url.startswith("https://")):
        return "Provide a valid http:// or https:// URL."
    try:
        subprocess.run(["cmd.exe", "/c", "start", "", url], check=False)
        return f"Launched in host browser: {url}"
    except FileNotFoundError:
        return f"Open manually: {url}"


def build_hud_header(
    router: ModelRouter,
    current_mode: str,
    current_task: str,
    loop_wait_sec: int,
    mock_enabled: bool,
    last_route: str,
    last_latency_s: float | None,
    due_count: int = 0,
) -> Panel:
    keys = ["GEMINI", "OPENROUTER", "GROQ", "MISTRAL", "DEEPSEEK", "ANTHROPIC", "TOGETHER", "FIREWORKS"]
    active_keys = [k for k in keys if os.getenv(f"{k}_API_KEY")]
    cds = router.get_active_cooldowns()
    latency_str = f"{last_latency_s:.2f}s" if last_latency_s is not None else "--"

    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)

    grid.add_row(
        f"[bold white]MODE[/]  [bold bright_cyan]{current_mode}[/]  [dim]|[/]  [bold white]TASK[/]  [bold bright_yellow]{current_task}[/]",
        f"[bold white]KEYS[/]  [bright_green]{len(active_keys)} active[/]  [dim]|[/]  [bold white]CD[/]  [{'bright_yellow' if cds else 'dim'}]{len(cds)}[/]  [dim]|[/]  [bold white]DUE[/]  [{'bright_red' if due_count else 'dim'}]{due_count}[/]",
        f"[bold white]LAST[/]  [cyan]{latency_str}[/]  [dim]|[/]  [bold white]LOOP[/]  [bright_magenta]{loop_wait_sec}s[/]  [bold white]MOCK[/]  [{'bright_yellow' if mock_enabled else 'dim'}]{'ON' if mock_enabled else 'OFF'}[/]",
    )
    grid.add_row(
        f"[bold white]ENGINE[/]  [bright_green]{'C++20 READY' if SCORER_BIN.exists() else 'MISSING'}[/]",
        f"[bold white]ROUTE[/]  [dim]{last_route[:44]}[/]",
        f"[bold white]TIP[/]  [dim]/help · Tab · ↑/↓ history[/]",
    )

    return Panel(
        grid,
        title="[bold bright_cyan]◈ KARLEXO v0.7 // AUTONOMOUS INTELLIGENCE COMMAND HUB ◈[/]",
        border_style="bright_cyan",
        box=box.ROUNDED,
        padding=(0, 1),
    )


def print_help_panel() -> None:
    help_md = """
### Routing & Reliability
* `/task <coding|debate|analysis|quant|general>` — switch task priority chain
* `/mode <NAME>` — switch persona/behavior profile
* `/loop <sec>` — auto-retry wait loop (`0` disables waiting)
* `/clear-cd` — clear all active provider cooldowns immediately
* `/mock` — toggle offline mock mode on/off

### Visuals, Feeds, Notes & Reminders
* `/chart 12,24,19,35` — render sparkline + bar chart
* `/pie A=60,B=25,C=15` — render proportion pie strip & table
* `/ratio 16:9` — compute simplified ratio, decimal & share %
* `/fx [amt] <BASE> <TARGET>` — live exchange rate (e.g. `/fx 250 USD INR`)
* `/news [world|india|markets|tech|<topic>]` — daily headlines
* `/note <tag>=<text>` & `/notes [tag]` — save & recall persistent notes
* `/remind <HH:MM|YYYY-MM-DD HH:MM> <msg>`, `/reminders`, `/done <id>`

### Intelligence, Execution & Export
* `/dig <query_or_url>` — fetch feeds/pages and rank signals with C++20 binary
* `/research <topic>` — dig + C++20 filter top signals + synthesize via `analysis` chain
* `/run <cpp|py> <code>` — compile & run C++20 or Python snippet locally
* `/export` — save last output to `~/karlexo/exports/`
* `/score` — run sample payload through C++20 signal ranker
* `/remember <key>=<value>`, `/memories`, `/reset-chat`
* `/save-link <alias>=<url>`, `/launch <alias>`, `/links`, `/status`, `/exit`
"""
    console.print(Panel(Markdown(help_md.strip()), title="[bold bright_cyan]Command Deck Reference[/]", border_style="cyan", box=box.ROUNDED))


def print_status_table(
    router: ModelRouter,
    current_mode: str,
    current_task: str,
    loop_wait_sec: int,
    mock_enabled: bool,
) -> None:
    table = Table(
        title="◈ KARLEXO Subsystem Telemetry ◈",
        border_style="bright_cyan",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold bright_cyan",
    )
    table.add_column("Subsystem", style="bold white", width=24)
    table.add_column("Status", style="bright_green")

    keys = ["GEMINI", "OPENROUTER", "GROQ", "MISTRAL", "DEEPSEEK", "ANTHROPIC", "TOGETHER", "FIREWORKS"]
    active_keys = [k for k in keys if os.getenv(f"{k}_API_KEY")]

    table.add_row("Active Mode / Task", f"{current_mode} / [bold bright_yellow]{current_task}[/]")
    table.add_row("Retry Loop / Mock", f"{loop_wait_sec}s / {'ENABLED' if mock_enabled else 'OFF'}")
    table.add_row("Active API Keys", ", ".join(active_keys) if active_keys else "[red]None[/]")
    table.add_row("C++20 Scorer Binary", "ONLINE" if SCORER_BIN.exists() else "[red]OFFLINE[/]")

    cds = router.get_active_cooldowns()
    if cds:
        cd_lines = [f"{ep} ({max(0, int(until - time.time()))}s: {reason})" for ep, (until, reason) in cds.items()]
        table.add_row("Active Cooldowns", "\n".join(cd_lines))
    else:
        table.add_row("Active Cooldowns", "None")

    console.print(table)


def main() -> None:
    conn = init_db()
    init_jarvis_tables(conn)
    router = ModelRouter(ROUTING_PATH, conn, timeout_sec=12.0)
    current_mode = "KARLEXO"
    current_task = "general"
    mock_enabled = False
    loop_wait_sec = 0
    last_route = "standby"
    last_latency_s: float | None = None
    last_output_md: str = ""

    commands = [
        "/mode", "/task", "/loop", "/clear-cd", "/dig", "/research",
        "/run", "/export", "/chart", "/pie", "/ratio", "/fx", "/news",
        "/note", "/notes", "/remind", "/reminders", "/done",
        "/save-link", "/launch", "/links", "/open",
        "/remember", "/memories", "/reset-chat", "/score", "/status",
        "/mock", "/help", "/exit",
        "coding", "debate", "analysis", "quant", "general", "cpp", "py",
    ]
    session = PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        completer=WordCompleter(commands, ignore_case=True),
    )

    console.clear()
    due_boot = get_due_reminders(conn)
    if due_boot:
        lines = [f"• [#{rid}] ({due}) {msg}" for rid, due, msg in due_boot]
        console.print(Panel("\n".join(lines), title="[bold bright_red]◈ Due Reminders ◈[/]", border_style="bright_red", box=box.ROUNDED))
    console.print(build_hud_header(router, current_mode, current_task, loop_wait_sec, mock_enabled, last_route, last_latency_s, len(due_boot)))

    while True:
        mock_tag = ":MOCK" if mock_enabled else ""
        loop_tag = f":LOOP{loop_wait_sec}s" if loop_wait_sec > 0 else ""
        ansi_prompt = ANSI(
            f"\x1b[1;36m[{current_mode}/\x1b[1;33m{current_task}{loop_tag}{mock_tag}\x1b[1;36m]\x1b[0m \x1b[1;37m>\x1b[0m "
        )
        try:
            raw_input = session.prompt(ansi_prompt)
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Shutting down KARLEXO...[/]")
            break

        user_input = sanitize_prompt_input(raw_input)
        if not user_input:
            continue

        decoded_input, decode_note = decode_local(user_input)
        if not mock_enabled and (
            any(ord(ch) > 127 for ch in decoded_input)
            or (decoded_input.startswith("/") and decoded_input.split()[0].lower() not in {"/mode","/task","/loop","/clear-cd","/dig","/research","/run","/export","/chart","/pie","/ratio","/fx","/news","/note","/notes","/remind","/reminders","/done","/save-link","/launch","/links","/open","/remember","/memories","/reset-chat","/score","/status","/mock","/help","/exit"})
        ):
            decoded_input, model_note = decode_with_model_fallback(router, user_input, decoded_input)
            if model_note:
                decode_note = model_note

        if decode_note:
            console.print(f"[dim italic]↳ {decode_note}[/]")
        user_input = decoded_input

        if user_input == "/exit":
            break

        if user_input == "/help":
            print_help_panel()
            continue

        if user_input == "/clear-cd":
            cleared = router.clear_cooldowns()
            console.print(f"[bright_green]◆ Cleared {cleared} endpoint cooldown(s).[/]")
            continue

        if user_input == "/reset-chat":
            conn.execute("DELETE FROM chat_history")
            conn.commit()
            console.print("[bright_green]◆ Cleared multi-turn chat history.[/]")
            continue

        if user_input == "/mock":
            mock_enabled = not mock_enabled
            console.print(f"[bright_yellow]◆ Mock mode {'ENABLED' if mock_enabled else 'DISABLED'}.[/]")
            continue

        if user_input.startswith("/loop "):
            raw_sec = user_input.split(" ", 1)[1].strip()
            if raw_sec.isdigit():
                loop_wait_sec = int(raw_sec)
                console.print(f"[bright_cyan]◆ Never-out retry wait set to {loop_wait_sec}s.[/]")
            else:
                console.print("[bright_red]✖ Format: /loop <seconds> (use 0 to disable waiting)[/]")
            continue

        if user_input.startswith("/mode "):
            requested = user_input.split(" ", 1)[1].strip().upper()
            if requested in MODES:
                current_mode = requested
                console.print(f"[bright_green]◆ Switched mode -> {current_mode}[/]")
            else:
                console.print(f"[bright_yellow]Available modes: {', '.join(MODES.keys())}[/]")
            continue

        if user_input.startswith("/task "):
            requested_task = user_input.split(" ", 1)[1].strip().lower()
            available_tasks = list(router.routing.get("tasks", {}).keys())
            if requested_task in available_tasks:
                current_task = requested_task
                console.print(f"[bright_green]◆ Switched task route -> {current_task}[/]")
            else:
                console.print(f"[bright_yellow]Available tasks: {', '.join(available_tasks)}[/]")
            continue

        if user_input.startswith("/run "):
            rest = user_input.split(" ", 1)[1].strip()
            parts = rest.split(None, 1)
            if len(parts) < 2:
                console.print("[bright_red]✖ Format: /run <cpp|py> <code>[/]")
                continue
            lang, code = parts[0].lower(), parts[1]
            with console.status(f"[bold bright_cyan]Running {lang} locally...[/]", spinner="dots12"):
                rc, out, elapsed = run_code_snippet(lang, code)
            border = "bright_green" if rc == 0 else "bright_red"
            console.print(
                Panel(
                    out,
                    title=f"[bold]◈ Sandbox ({lang}) · exit={rc} · {elapsed:.2f}s ◈[/]",
                    border_style=border,
                    box=box.ROUNDED,
                )
            )
            continue

        if user_input == "/export":
            if not last_output_md:
                console.print("[bright_yellow]Nothing generated yet to export.[/]")
                continue
            EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_file = EXPORTS_DIR / f"karlexo_{stamp}.md"
            out_file.write_text(f"# KARLEXO Export ({stamp})\n\nRoute: `{last_route}`\n\n{last_output_md}\n")
            console.print(f"[bright_green]◆ Exported to:[/] {out_file}")
            continue

        if user_input.startswith("/chart "):
            raw_vals = user_input.split(" ", 1)[1].strip()
            parts = [p.strip() for p in re.split(r"[,\s]+", raw_vals) if p.strip()]
            labels: list[str] = []
            nums: list[float] = []
            for p in parts:
                if "=" in p:
                    k, v = p.split("=", 1)
                    try:
                        nums.append(float(v))
                        labels.append(k[:12])
                    except ValueError:
                        pass
                else:
                    try:
                        nums.append(float(p))
                        labels.append(f"#{len(nums)}")
                    except ValueError:
                        pass
            chart_txt = render_bar_chart(nums, labels)
            last_output_md = chart_txt
            console.print(Panel(chart_txt, title="[bold bright_cyan]◈ Chart ◈[/]", border_style="cyan", box=box.ROUNDED))
            continue

        if user_input.startswith("/pie "):
            raw_pie = user_input.split(" ", 1)[1].strip()
            slices: list[tuple[str, float]] = []
            for item in [x.strip() for x in raw_pie.split(",") if x.strip()]:
                if "=" in item:
                    k, v = item.split("=", 1)
                    try:
                        slices.append((k.strip(), float(v.strip())))
                    except ValueError:
                        pass
            pie_txt = render_pie_chart(slices)
            last_output_md = pie_txt
            console.print(Panel(pie_txt, title="[bold bright_cyan]◈ Pie Breakdown ◈[/]", border_style="cyan", box=box.ROUNDED))
            continue

        if user_input.startswith("/ratio "):
            raw_r = user_input.split(" ", 1)[1].strip().replace(":", " ").replace("/", " ")
            toks = [t for t in raw_r.split() if t]
            if len(toks) >= 2:
                try:
                    r_txt = render_ratio(float(toks[0]), float(toks[1]))
                    last_output_md = r_txt
                    console.print(Panel(r_txt, title="[bold bright_cyan]◈ Ratio ◈[/]", border_style="cyan", box=box.ROUNDED))
                except ValueError:
                    console.print("[bright_red]✖ Format: /ratio 16:9 or /ratio 120 45[/]")
            else:
                console.print("[bright_red]✖ Format: /ratio 16:9 or /ratio 120 45[/]")
            continue

        if user_input == "/fx" or user_input.startswith("/fx "):
            raw_fx = user_input[3:].strip()
            amt, base_c, tgt_c = parse_fx_args(raw_fx)
            try:
                with console.status(f"[bold bright_cyan]Fetching {base_c}->{tgt_c} rates...[/]", spinner="dots12"):
                    fx_txt = fetch_fx(amt, base_c, tgt_c)
                last_output_md = fx_txt
                console.print(Panel(fx_txt, title="[bold bright_cyan]◈ Currency Feed ◈[/]", border_style="cyan", box=box.ROUNDED))
            except Exception as exc:
                console.print(f"[bright_red]✖ FX lookup failed: {exc}[/]")
            continue

        if user_input == "/news" or user_input.startswith("/news "):
            topic = user_input.split(" ", 1)[1].strip() if " " in user_input else "world"
            with console.status(f"[bold bright_cyan]Fetching '{topic}' headlines...[/]", spinner="dots12"):
                news_txt = fetch_daily_news(topic)
            last_output_md = news_txt
            console.print(Panel(news_txt, title="[bold bright_cyan]◈ Daily News Briefing ◈[/]", border_style="cyan", box=box.ROUNDED))
            continue

        if user_input.startswith("/note "):
            payload = user_input.split(" ", 1)[1].strip()
            if "=" in payload:
                tag, body = payload.split("=", 1)
                nid = add_note(conn, tag, body)
            else:
                nid = add_note(conn, "general", payload)
            console.print(f"[bright_green]◆ Note #{nid} saved to memory.db[/]")
            continue

        if user_input == "/notes" or user_input.startswith("/notes "):
            flt = user_input.split(" ", 1)[1].strip() if " " in user_input else ""
            rows = list_notes(conn, flt)
            if not rows:
                console.print("[dim]No notes found. Save one with /note tag=your note[/]")
            else:
                ntable = Table(title="Saved Notes", border_style="cyan", box=box.ROUNDED)
                ntable.add_column("ID", style="bold bright_yellow", width=5)
                ntable.add_column("Tag", style="bold bright_cyan", width=12)
                ntable.add_column("Note", style="white")
                for nid, tag, body, _ in rows:
                    ntable.add_row(str(nid), tag, body)
                console.print(ntable)
            continue

        if user_input.startswith("/remind "):
            rest = user_input.split(" ", 1)[1].strip()
            m = re.match(r"^(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2})\s+(.+)$", rest)
            if not m:
                console.print("[bright_red]✖ Format: /remind 18:30 Review quant notes  (or /remind 2026-11-27 09:00 Mock test)[/]")
                continue
            final_due = add_reminder(conn, m.group(1), m.group(2))
            console.print(f"[bright_green]◆ Reminder set for {final_due}:[/] {m.group(2).strip()}")
            continue

        if user_input == "/reminders":
            rows = get_reminders(conn, include_done=False)
            if not rows:
                console.print("[dim]No active reminders.[/]")
            else:
                rtable = Table(title="Active Reminders", border_style="bright_yellow", box=box.ROUNDED)
                rtable.add_column("ID", style="bold bright_yellow", width=5)
                rtable.add_column("Due At", style="bold bright_cyan", width=18)
                rtable.add_column("Reminder", style="white")
                for rid, due, msg, _ in rows:
                    rtable.add_row(str(rid), due, msg)
                console.print(rtable)
            continue

        if user_input.startswith("/done "):
            rid_str = user_input.split(" ", 1)[1].strip()
            if rid_str.isdigit() and mark_reminder_done(conn, int(rid_str)):
                console.print(f"[bright_green]◆ Marked reminder #{rid_str} complete.[/]")
            else:
                console.print(f"[bright_yellow]No reminder found with ID '{rid_str}'.[/]")
            continue

        if user_input.startswith("/dig "):
            query = user_input.split(" ", 1)[1].strip()
            t0 = time.perf_counter()
            with console.status(f"[bold bright_cyan]Digging & C++20 scoring: {query}...[/]", spinner="dots12"):
                result = dig_and_score(query, SCORER_BIN)
            last_latency_s = time.perf_counter() - t0
            last_route = f"DIG+CPP20 ({last_latency_s:.2f}s)"
            last_output_md = result
            console.print(
                Panel(
                    result,
                    title=f"[bold bright_cyan]◈ Signal Dig & C++20 Rank: {query} ◈[/]",
                    subtitle=f"[dim]Elapsed: {last_latency_s:.2f}s[/]",
                    border_style="cyan",
                    box=box.ROUNDED,
                )
            )
            continue

        if user_input.startswith("/save-link "):
            pair = user_input.split(" ", 1)[1]
            if "=" in pair:
                alias, url = [x.strip() for x in pair.split("=", 1)]
                conn.execute(
                    "INSERT INTO bookmarks (alias, url) VALUES (?, ?) "
                    "ON CONFLICT(alias) DO UPDATE SET url=excluded.url",
                    (alias.lower(), url),
                )
                conn.commit()
                console.print(f"[bright_green]◆ Bookmarked:[/] [bold]{alias.lower()}[/] -> {url}")
            else:
                console.print("[bright_red]✖ Format: /save-link alias=https://example.com[/]")
            continue

        if user_input == "/links":
            rows = conn.execute("SELECT alias, url FROM bookmarks ORDER BY alias").fetchall()
            if not rows:
                console.print("[dim]No bookmarks saved yet. Use /save-link alias=https://...[/]")
            else:
                btable = Table(title="Saved Bookmarks", border_style="cyan", box=box.ROUNDED)
                btable.add_column("Alias", style="bold bright_cyan")
                btable.add_column("URL", style="white")
                for alias, url in rows:
                    btable.add_row(alias, url)
                console.print(btable)
            continue

        if user_input.startswith("/launch "):
            alias = user_input.split(" ", 1)[1].strip().lower()
            row = conn.execute("SELECT url FROM bookmarks WHERE alias = ?", (alias,)).fetchone()
            if row:
                console.print(f"[bright_green]◆ {open_url_in_host(row[0])}[/]")
            else:
                all_bms = conn.execute("SELECT alias FROM bookmarks").fetchall()
                names = ", ".join(r[0] for r in all_bms) or "none saved yet"
                console.print(f"[bright_yellow]Unknown alias '{alias}'. Saved: {names}[/]")
            continue

        if user_input.startswith("/open "):
            url = user_input.split(" ", 1)[1].strip()
            console.print(f"[bright_green]◆ {open_url_in_host(url)}[/]")
            continue

        if user_input.startswith("/remember "):
            pair = user_input.split(" ", 1)[1]
            if "=" in pair:
                k, v = [x.strip() for x in pair.split("=", 1)]
                conn.execute(
                    "INSERT INTO memory (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, v),
                )
                conn.commit()
                console.print(f"[bright_green]◆ Saved memory:[/] [bold]{k}[/] -> {v}")
            else:
                console.print("[bright_red]✖ Format: /remember topic=details[/]")
            continue

        if user_input == "/memories":
            console.print(
                Panel(load_memory_context(conn), title="[bold bright_cyan]◈ Persistent Memory ◈[/]", border_style="cyan", box=box.ROUNDED)
            )
            continue

        if user_input == "/score":
            sample = (
                "AI|New open-weights reasoning model beats frontier benchmarks|9|9|8\n"
                "MEME_COIN|Anonymous token pumps 400% on Telegram rumors|8|5|2\n"
                "MACRO|Central bank signals rate hold amid sticky services inflation|7|8|9\n"
            )
            console.print(
                Panel(rank_signals_with_cpp(sample), title="[bold bright_cyan]◈ C++20 Signal Ranker ◈[/]", border_style="cyan", box=box.ROUNDED)
            )
            continue

        if user_input == "/status":
            print_status_table(router, current_mode, current_task, loop_wait_sec, mock_enabled)
            continue

        hint = suggest_task_if_general(current_task, user_input)
        if hint:
            console.print(f"[dim]Tip: This looks like a [bold]{hint}[/] prompt — switch anytime with [bold]/task {hint}[/].[/]")

        prompt_to_send = user_input
        task_to_use = current_task

        if user_input.startswith("/research "):
            research_topic = user_input.split(" ", 1)[1].strip()
            with console.status(f"[bold bright_cyan]Digging & C++20 ranking signals for '{research_topic}'...[/]", spinner="dots12"):
                scored_digest = dig_and_score(research_topic, SCORER_BIN)
            top_lines = "\n".join(scored_digest.splitlines()[:8])
            console.print(
                Panel(
                    top_lines,
                    title=f"[bold bright_yellow]◈ Top C++20 Signals: {research_topic} ◈[/]",
                    border_style="yellow",
                    box=box.ROUNDED,
                )
            )
            task_to_use = "analysis"
            prompt_to_send = (
                f"Research Topic: {research_topic}\n"
                f"Top C++20 Ranked Signals:\n{top_lines}\n\n"
                "Provide a crisp, structured synthesis and key takeaways."
            )

        memory_text = load_memory_context(conn)
        recent_chat = load_recent_chat(conn, limit=16)
        history_block = f"\n\nRecent Session Context:\n{recent_chat}" if recent_chat else ""
        system_prompt = (
            f"{MODES[current_mode]}\n"
            f"Active Task Profile: {task_to_use}\n\n"
            f"User Memory Context:\n{memory_text}{history_block}"
        )

        VISUAL_RULES_PATH = BASE_DIR / "visual_rules.md"


def _build_visual_system_prompt(base_sys: str, prompt_text: str):
    try:
        from visual_router import classify_visual_need
        decision = classify_visual_need(prompt_text)
    except Exception:
        return base_sys, None
    if not (decision.render_chart or decision.render_table):
        return base_sys, decision
    rules_txt = VISUAL_RULES_PATH.read_text(encoding="utf-8") if VISUAL_RULES_PATH.exists() else ""
    extra = (
        f"{rules_txt}\n"
        "[AUTO-VISUAL OUTPUT HINT]\n"
        "- Include a clean comparison table for multi-horizon or multi-item figures.\n"
        "- When giving numeric series or allocations, you may also include a single line "
        "`CHART_DATA: 1W=1.2, 1M=3.4, 1Y=12.5` or `PIE_DATA: Equity=60, Gold=25, Debt=15` "
        "so KARLEXO renders a visual panel automatically."
    )
    merged = f"{base_sys}\n\n{extra}" if base_sys else extra
    return merged, decision


def _extract_visual_panels(reply_text: str, decision):
    panels = []
    if not decision or not decision.render_chart:
        return panels
    chart_m = re.search(r"^CHART_DATA:\s*(.+)$", reply_text, re.MULTILINE | re.IGNORECASE)
    pie_m = re.search(r"^PIE_DATA:\s*(.+)$", reply_text, re.MULTILINE | re.IGNORECASE)

    def _parse_pairs(raw: str):
        labels, vals, slices = [], [], []
        for chunk in re.split(r"[,|]", raw):
            if "=" in chunk or ":" in chunk:
                sep = "=" if "=" in chunk else ":"
                k, v = chunk.split(sep, 1)
                num_m = re.search(r"[-+]?\d+(?:\.\d+)?", v)
                if num_m:
                    lbl = k.strip()[:14]
                    val = float(num_m.group(0))
                    labels.append(lbl)
                    vals.append(val)
                    if val > 0:
                        slices.append((lbl, val))
        return labels, vals, slices

    if pie_m:
        _, _, slices = _parse_pairs(pie_m.group(1))
        if len(slices) >= 2:
            panels.append(("Pie Breakdown", render_pie_chart(slices)))
    if chart_m:
        labels, vals, _ = _parse_pairs(chart_m.group(1))
        if len(vals) >= 2:
            panels.append(("Chart", render_bar_chart(vals, labels)))
    if not panels:
        pairs = re.findall(
            r"\b(1W|1M|3M|6M|1Y|3Y|5Y|10Y|YTD|Equity|Gold|Silver|Debt|Cash)\b[^\n\-\d+]*([-+]?\d+(?:\.\d+)?)\s*%",
            reply_text,
            re.IGNORECASE,
        )
        seen = set()
        labels, vals = [], []
        for k, v in pairs:
            key = k.upper()
            if key not in seen:
                seen.add(key)
                labels.append(key[:10])
                vals.append(float(v))
        if len(vals) >= 2:
            panels.append(("Auto Horizon / Breakdown", render_bar_chart(vals, labels)))
    return panels


def _mock_fn(p: str) -> str:
            return (
                f"**[MOCK/{current_mode}/{task_to_use}]** Prompt: `{p[:220]}`\n\n"
                f"C++ scorer binary ready: `{SCORER_BIN.exists()}` | Memories loaded."
            )

        t0 = time.perf_counter()
        effective_system_prompt, vis_decision = _build_visual_system_prompt(system_prompt, user_input)
        with console.status(f"[bold bright_cyan]Dispatching via {task_to_use} chain...[/]", spinner="dots12"):
            reply, route_used = router.route_generate(
                task=task_to_use,
                system_prompt=effective_system_prompt,
                prompt=prompt_to_send,
                mock_fallback_fn=_mock_fn,
                force_mock=mock_enabled,
                loop_wait_sec=loop_wait_sec,
            )
        last_latency_s = time.perf_counter() - t0
        last_route = route_used

        formatted = clean_terminal_markdown(reply)
        last_output_md = formatted
        append_chat(conn, "user", task_to_use, "input", user_input)
        append_chat(conn, "assistant", task_to_use, route_used, formatted)

        border = "yellow" if "MOCK" in route_used else "bright_cyan"
        due_now = len(get_due_reminders(conn))
        console.print()
        console.print(
            Panel(
                Markdown(formatted),
                title=f"[bold]◈ {route_used} ◈[/]",
                subtitle=f"[dim]task={task_to_use} · latency={last_latency_s:.2f}s · /export to save[/]",
                border_style=border,
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        console.print(
            build_hud_header(router, current_mode, current_task, loop_wait_sec, mock_enabled, last_route, last_latency_s, due_now)
        )


if __name__ == "__main__":
    main()
