import difflib
import json
import re
from typing import Tuple

SLASH_COMMANDS = [
    "/mode", "/task", "/loop", "/clear-cd", "/dig", "/research",
    "/run", "/export", "/chart", "/pie", "/ratio", "/fx", "/news",
    "/note", "/notes", "/remind", "/reminders", "/done",
    "/save-link", "/launch", "/links", "/open",
    "/remember", "/memories", "/reset-chat", "/score", "/status",
    "/mock", "/help", "/exit",
]

COMMAND_ALIASES = {
    "/chrt": "/chart", "/graph": "/chart", "/plot": "/chart", "/bar": "/chart",
    "/piechart": "/pie", "/share": "/pie", "/split": "/pie",
    "/rat": "/ratio", "/prop": "/ratio", "/odds": "/ratio",
    "/curr": "/fx", "/currency": "/fx", "/conv": "/fx", "/rate": "/fx",
    "/khabar": "/news", "/headlines": "/news", "/samachar": "/news",
    "/nts": "/note", "/likh": "/note", "/save": "/note",
    "/rem": "/remind", "/alarm": "/remind", "/yaad": "/remind",
    "/rems": "/reminders", "/todo": "/reminders", "/tasks": "/reminders",
    "/fin": "/done", "/complete": "/done", "/hogaya": "/done",
    "/st": "/status", "/info": "/status", "/health": "/status",
    "/clr": "/clear-cd", "/unblock": "/clear-cd",
    "/exp": "/export", "/md": "/export",
    "/hlp": "/help", "/cmds": "/help",
    "/q": "/exit", "/bye": "/exit", "/quit": "/exit",
}

SLANG_REPLACEMENTS = {
    r"\bpls\b": "please",
    r"\bplz\b": "please",
    r"\bu\b": "you",
    r"\bur\b": "your",
    r"\br\b": "are",
    r"\bmsg\b": "message",
    r"\binfo\b": "information",
    r"\bcalc\b": "calculate",
    r"\bexp\b": "explain",
    r"\bbtao\b": "tell",
    r"\bbatao\b": "tell",
    r"\bdikha\b": "show",
    r"\bdikhao\b": "show",
    r"\bsamjha\b": "explain",
    r"\bsamjhao\b": "explain",
    r"\bkhol\b": "open",
    r"\bkholo\b": "open",
    r"\byaad dila\b": "remind",
    r"\byaad dilao\b": "remind",
    r"\bpaise\b": "currency",
    r"\brupaye\b": "inr",
    r"\bdollar\b": "usd",
    r"\bkhabar\b": "news",
    r"\bhaal chal\b": "status",
    r"\bkya chal raha\b": "status",
}


def normalize_shorthand(text: str) -> str:
    out = text.strip()
    out = re.sub(r"(?<=\d)[kK]\b", "000", out)
    out = re.sub(r"(?<=\d)[mM]\b", "000000", out)
    for pat, rep in SLANG_REPLACEMENTS.items():
        out = re.sub(pat, rep, out, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", out).strip()


def decode_local(raw_text: str) -> Tuple[str, str | None]:
    """
    Returns (normalized_text, decoded_note_or_None).
    Never raises errors; always returns a usable string.
    """
    cleaned = normalize_shorthand(raw_text)
    if not cleaned:
        return "", None

    if cleaned.startswith("/"):
        parts = cleaned.split(None, 1)
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if cmd in SLASH_COMMANDS:
            if cmd != cleaned.split()[0]:
                return f"{cmd} {rest}".strip(), f"normalized command -> {cmd}"
            return cleaned, None

        if cmd in COMMAND_ALIASES:
            mapped = COMMAND_ALIASES[cmd]
            final = f"{mapped} {rest}".strip()
            return final, f"alias {cmd} -> {final}"

        match = difflib.get_close_matches(cmd, SLASH_COMMANDS, n=1, cutoff=0.55)
        if match:
            fixed_cmd = match[0]
            final = f"{fixed_cmd} {rest}".strip()
            return final, f"typo {cmd} -> {final}"

        # Unknown slash command becomes natural language instead of an error
        natural = f"{cmd.lstrip('/')} {rest}".strip()
        return natural, f"treated unknown slash '{cmd}' as prompt"

    low = cleaned.lower()

    if low in ("status", "haal", "kaisa hai", "kya haal"):
        return "/status", f"decoded '{raw_text}' -> /status"
    if low in ("help", "madad", "commands", "kya kar sakte ho"):
        return "/help", f"decoded '{raw_text}' -> /help"
    if low in ("notes", "mere notes", "note dikhao"):
        return "/notes", f"decoded '{raw_text}' -> /notes"
    if low in ("reminders", "yaad", "mere reminders", "kya karna hai"):
        return "/reminders", f"decoded '{raw_text}' -> /reminders"
    if low in ("news", "aaj ki khabar", "taza khabar"):
        return "/news", f"decoded '{raw_text}' -> /news"
    if low in ("export", "save chat", "md banao"):
        return "/export", f"decoded '{raw_text}' -> /export"
    if low in ("exit", "quit", "bye", "band kar", "nikal"):
        return "/exit", f"decoded '{raw_text}' -> /exit"

    # Natural FX phrasing: "1000 usd to inr", "500 dollar in rupees", "convert 200 eur to usd"
    fx_m = re.search(
        r"(?:convert\s+|badlo\s+)?(\d+(?:\.\d+)?)\s*(usd|inr|eur|gbp|jpy|aed|sgd|aud|cad|chf)\s*(?:to|in|mein|mei|se|ko)\s*(usd|inr|eur|gbp|jpy|aed|sgd|aud|cad|chf)",
        low,
    )
    if fx_m:
        amt, b, t = fx_m.group(1), fx_m.group(2).upper(), fx_m.group(3).upper()
        return f"/fx {amt} {b} {t}", f"decoded FX -> /fx {amt} {b} {t}"

    # Natural chart phrasing: "chart 10, 20, 30"
    ch_m = re.match(r"^(?:chart|graph|plot)\s+(.+)$", low)
    if ch_m:
        return f"/chart {ch_m.group(1).strip()}", f"decoded chart -> /chart {ch_m.group(1).strip()}"

    # Natural note phrasing: "note tag=text" or "note text"
    nt_m = re.match(r"^(?:note|likh\s*lo|save\s*karo)\s+(.+)$", cleaned, flags=re.IGNORECASE)
    if nt_m:
        body = nt_m.group(1).strip()
        if "=" not in body:
            body = f"quick={body}"
        return f"/note {body}", f"decoded note -> /note {body}"

    # Natural reminder phrasing: "remind 18:00 do homework" or "18:00 yaad dilana run"
    rm_m = re.match(r"^(?:remind|yaad\s*dila\w*)\s+(.+)$", cleaned, flags=re.IGNORECASE)
    if rm_m:
        return f"/remind {rm_m.group(1).strip()}", f"decoded reminder -> /remind"

    if cleaned != raw_text.strip():
        return cleaned, f"expanded shorthand/lang -> {cleaned}"
    return cleaned, None


def decode_with_model_fallback(
    router,
    raw_text: str,
    local_text: str,
) -> Tuple[str, str | None]:
    """
    If local_text still looks like a garbled command or non-English request that
    benefits from translation/command mapping, ask the router to normalize it.
    Otherwise pass local_text straight through as a normal prompt.
    """
    has_non_ascii = any(ord(ch) > 127 for ch in local_text)
    looks_broken_cmd = local_text.startswith("/") and local_text.split()[0].lower() not in SLASH_COMMANDS

    if not has_non_ascii and not looks_broken_cmd:
        return local_text, None

    sys_p = (
        "You are an intent normalizer for a CLI assistant called KARLEXO. "
        "Translate Hindi/Hinglish/slang/garbled input into a clear, concise English prompt "
        "OR a valid KARLEXO slash command if exact. Available commands: "
        + ", ".join(SLASH_COMMANDS)
        + ". Return JSON only: {\"action\": \"...\", \"note\": \"...\"}"
    )
    try:
        out, _ = router.route_generate(
            task="general",
            system_prompt=sys_p,
            prompt=f"Raw input: {raw_text}\nNormalized: {local_text}",
            mock_fallback_fn=lambda _: json.dumps({"action": local_text, "note": "fallback"}),
            force_mock=False,
            loop_wait_sec=0,
        )
        m = re.search(r"\{.*\}", out, flags=re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            act = str(data.get("action", "")).strip()
            nt = str(data.get("note", "model decoded")).strip()
            if act:
                return act, f"AI decoded -> {act} ({nt})"
    except Exception:
        pass

    return local_text, None
