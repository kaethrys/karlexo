try:
    from brain.polyglot import polyglot_directive
    from brain.persona import persona_directive
except ImportError:
    from polyglot import polyglot_directive
    from persona import persona_directive
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


class ModelRouter:
    def __init__(self, routing_path: Path, db_conn: sqlite3.Connection, timeout_sec: float = 12.0) -> None:
        self.routing_path = routing_path
        self.routing = json.loads(routing_path.read_text())
        self.conn = db_conn
        self.timeout_sec = timeout_sec
        self._init_table()

    def _init_table(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS endpoint_cooldowns (
                endpoint_key TEXT PRIMARY KEY,
                cooldown_until REAL NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def _is_cooled_down(self, endpoint_key: str) -> Tuple[bool, str]:
        now = time.time()
        row = self.conn.execute(
            "SELECT cooldown_until, reason FROM endpoint_cooldowns WHERE endpoint_key = ?",
            (endpoint_key,),
        ).fetchone()
        if not row:
            return False, ""
        until, reason = float(row[0]), str(row[1])
        if now < until:
            return True, reason
        self.conn.execute("DELETE FROM endpoint_cooldowns WHERE endpoint_key = ?", (endpoint_key,))
        self.conn.commit()
        return False, ""

    def _set_cooldown(self, endpoint_key: str, seconds: int, reason: str) -> None:
        until = time.time() + max(5, int(seconds))
        self.conn.execute(
            """
            INSERT INTO endpoint_cooldowns (endpoint_key, cooldown_until, reason)
            VALUES (?, ?, ?)
            ON CONFLICT(endpoint_key) DO UPDATE SET
                cooldown_until = excluded.cooldown_until,
                reason = excluded.reason
            """,
            (endpoint_key, until, reason[:140]),
        )
        self.conn.commit()

    def clear_cooldowns(self) -> int:
        cur = self.conn.execute("DELETE FROM endpoint_cooldowns")
        self.conn.commit()
        return cur.rowcount

    def get_active_cooldowns(self) -> Dict[str, Tuple[float, str]]:
        now = time.time()
        rows = self.conn.execute(
            "SELECT endpoint_key, cooldown_until, reason FROM endpoint_cooldowns WHERE cooldown_until > ?",
            (now,),
        ).fetchall()
        return {str(k): (float(u), str(r)) for k, u, r in rows}

    def _post_json(self, url: str, headers: Dict[str, str], payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)

    @staticmethod
    def _compact_error(exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            return f"HTTP {exc.code}"
        if isinstance(exc, urllib.error.URLError):
            reason = str(exc.reason)
            if "timed out" in reason.lower():
                return "timeout"
            if "111" in reason or "refused" in reason.lower():
                return "offline"
            return reason[:45]
        msg = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        return msg[:45]

    def _call_gemini(self, model: str, system_prompt: str, prompt: str) -> str:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY missing")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        }
        data = self._post_json(url, {"Content-Type": "application/json"}, payload)
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError("empty candidates")
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        if not text:
            raise RuntimeError("empty text")
        return text

    def _call_anthropic(self, provider_cfg: dict, model: str, system_prompt: str, prompt: str) -> str:
        env_key = provider_cfg.get("env_key", "ANTHROPIC_API_KEY")
        api_key = os.getenv(env_key, "").strip()
        if not api_key:
            raise RuntimeError(f"{env_key} missing")
        url = provider_cfg["base_url"]
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": provider_cfg.get("api_version", "2023-06-01"),
        }
        payload = {
            "model": model,
            "max_tokens": 2048,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
        }
        data = self._post_json(url, headers, payload)
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
        if not text:
            raise RuntimeError("empty content")
        return text

    def _call_openai_compat(self, provider_name: str, provider_cfg: dict, model: str, system_prompt: str, prompt: str) -> str:
        env_key = provider_cfg.get("env_key", "")
        api_key = os.getenv(env_key, "").strip() if env_key else ""
        optional_auth = bool(provider_cfg.get("optional_auth", False))
        if not api_key and not optional_auth:
            raise RuntimeError(f"{env_key} missing")

        url = provider_cfg["base_url"]
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if provider_name == "openrouter":
            headers["HTTP-Referer"] = "https://karlexo.local"
            headers["X-Title"] = "KARLEXO Command Hub"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        data = self._post_json(url, headers, payload)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("empty choices")
        msg = choices[0].get("message") or {}
        text = (msg.get("content") or "").strip()
        if not text:
            raise RuntimeError("empty content")
        return text

    def _try_endpoint(self, spec: dict, system_prompt: str, prompt: str) -> Tuple[Optional[str], str]:
        provider = spec["provider"]
        model = spec["model"]
        cooldown_sec = int(spec.get("cooldown_sec", 60))
        endpoint_key = f"{provider}:{model}"

        cooled, reason = self._is_cooled_down(endpoint_key)
        if cooled:
            return None, f"{endpoint_key} [cd:{reason}]"

        try:
            if provider == "gemini":
                text = self._call_gemini(model, system_prompt, prompt)
            elif provider == "anthropic":
                p_cfg = (self.routing.get("providers") or {}).get(provider, {})
                text = self._call_anthropic(p_cfg, model, system_prompt, prompt)
            else:
                p_cfg = (self.routing.get("providers") or {}).get(provider)
                if not p_cfg:
                    raise RuntimeError("unknown provider")
                text = self._call_openai_compat(provider, p_cfg, model, system_prompt, prompt)
            return text, endpoint_key
        except Exception as exc:
            err = self._compact_error(exc)
            cd_time = 25 if "timeout" in err or "offline" in err else cooldown_sec
            self._set_cooldown(endpoint_key, cd_time, err)
            return None, f"{endpoint_key} [{err}]"

    def _build_pool(self, task: str) -> List[dict]:
        tasks_map = self.routing.get("tasks", {})
        primary = list(tasks_map.get(task) or tasks_map.get(self.routing.get("default_task", "general"), []))
        seen = {(x.get("provider"), x.get("model")) for x in primary}
        for t_name, chain in tasks_map.items():
            if t_name == task:
                continue
            for item in chain:
                key = (item.get("provider"), item.get("model"))
                if key not in seen:
                    seen.add(key)
                    primary.append(item)
        return primary

    def route_generate(
        self,
        task: str,
        system_prompt: str,
        prompt: str,
        mock_fallback_fn: Callable[[str], str],
        force_mock: bool = False,
        loop_wait_sec: int = 0,
    ) -> Tuple[str, str]:
        # KARLEXO_POLYGLOT_SPEED_HOOK
        _persona_rule = persona_directive(prompt or "")
        _speed_rule = "Be crisp, direct, and high-signal; avoid filler unless asked for detail."
        _pg_rule = polyglot_directive(prompt or "")
        _extras = [x for x in (_persona_rule, _speed_rule, _pg_rule) if x and x not in (system_prompt or "")]
        if _extras:
            system_prompt = (system_prompt + "\n\n" if system_prompt else "") + "\n".join(_extras)

        if force_mock:
            return mock_fallback_fn(prompt), f"MOCK (manual, task={task})"

        pool = self._build_pool(task)
        if not pool:
            return mock_fallback_fn(prompt), f"MOCK (empty pool, task={task})"

        deadline = time.time() + max(0, int(loop_wait_sec))
        attempt = 0
        last_notes: List[str] = []

        while True:
            attempt += 1
            notes: List[str] = []
            for spec in pool:
                text, note = self._try_endpoint(spec, system_prompt, prompt)
                notes.append(note)
                if text:
                    suffix = f" · retry#{attempt}" if attempt > 1 else ""
                    return text, f"{note} · {task}{suffix}"
            last_notes = notes

            remaining = deadline - time.time()
            if remaining <= 0:
                break
            sleep_for = min(8.0, remaining)
            time.sleep(sleep_for)

        compact_summary = " → ".join(last_notes[-6:]) if last_notes else "no endpoints"
        fallback_body = mock_fallback_fn(prompt)
        return (
            f"{fallback_body}\n\n---\n*Auto-failover summary:* `{compact_summary}`",
            f"MOCK (failover, task={task})",
        )
