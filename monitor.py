#!/usr/bin/env python3
"""
Webpage / connectivity monitoring with Pushover alerts.
Designed for periodic runs (e.g. every 15 min via Docker or systemd).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
import yaml


@dataclass
class CheckResult:
    name: str
    key: str
    ok: bool
    detail: str
    push_on_success: bool = False


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    return data if isinstance(data, dict) else {}


def merge_pages_from_file(config: dict[str, Any], config_path: Path) -> None:
    """Lädt Seiten aus pages_file, falls gesetzt; sonst bleiben inline-pages."""
    ref = config.get("pages_file")
    if not ref:
        return
    pages_path = (config_path.parent / str(ref)).resolve()
    if not pages_path.exists():
        raise FileNotFoundError(
            f"pages_file nicht gefunden: {pages_path} (gesetzt in {config_path})"
        )
    sub = load_config(pages_path)
    pages = sub.get("pages")
    if pages is None:
        raise ValueError(
            f"{pages_path} muss einen Schlüssel „pages“ mit einer Liste enthalten."
        )
    if config.get("pages"):
        raise ValueError(
            "Entweder „pages“ in config.yaml oder „pages_file“ — nicht beides."
        )
    config["pages"] = pages


def check_body_expectations(entry: dict[str, Any], text: str) -> tuple[bool, str | None]:
    """
    expect_body_contains: str oder Liste — alle Teilstrings müssen vorkommen.
    expect_body_regex: str oder Liste — jedes Muster muss mit re.search matchen.
    """
    raw = entry.get("expect_body_contains")
    if raw is not None:
        needles = [raw] if isinstance(raw, str) else list(raw)
        for needle in needles:
            if needle not in text:
                return False, f"Antwort enthält nicht: {needle!r}"

    regex_raw = entry.get("expect_body_regex")
    if regex_raw is not None:
        patterns = [regex_raw] if isinstance(regex_raw, str) else list(regex_raw)
        for pat in patterns:
            try:
                if not re.search(pat, text, re.DOTALL):
                    return False, f"Regex matched nicht: {pat!r}"
            except re.error as e:
                return False, f"Ungültiges Regex {pat!r}: {e}"

    return True, None


def html_has_id_anchor(html: str, anchor: str) -> bool:
    """True, wenn irgendwo id="<anchor>" (Groß/Kleinschreibung egal) vorkommt."""
    want = anchor.lower()
    for m in re.finditer(r'\bid\s*=\s*(["\'])([^"\']*)\1', html, re.IGNORECASE):
        if m.group(2).lower() == want:
            return True
    return False


def check_anchor_expectations(entry: dict[str, Any], html: str) -> tuple[bool, str | None]:
    """anchor: ein id-Wert; anchors: Liste — alle müssen vorkommen."""
    one = entry.get("anchor")
    many = entry.get("anchors")
    if many is not None:
        ids = [many] if isinstance(many, str) else list(many)
        for aid in ids:
            if not html_has_id_anchor(html, str(aid)):
                return False, f"Anker id={aid!r} fehlt im HTML"
        return True, None
    if one is not None:
        if not html_has_id_anchor(html, str(one)):
            return False, f"Anker id={one!r} fehlt im HTML"
    return True, None


def merge_nav_entry(parent: dict[str, Any], nav: dict[str, Any]) -> dict[str, Any]:
    """Erbt verify_tls, timeout_seconds, expect_status von der übergeordneten Seite."""
    out: dict[str, Any] = {}
    for k in ("expect_status", "timeout_seconds", "verify_tls"):
        if k in parent:
            out[k] = parent[k]
    out.update({k: v for k, v in nav.items() if k != "path"})
    base = parent["url"]
    if "url" in nav:
        out["url"] = nav["url"]
    elif "path" in nav:
        out["url"] = urljoin(base, nav["path"])
    else:
        out["url"] = base
    plabel = parent.get("name") or base
    nlabel = nav.get("name", "Navigation")
    out["name"] = f"{plabel} › {nlabel}"
    return out


def state_path_from_config(config: dict[str, Any], config_file: Path) -> Path:
    env_state = os.environ.get("WEBPAGE_WATCHER_STATE_FILE")
    if env_state:
        return Path(env_state).expanduser()
    raw = config.get("state_file")
    if raw:
        return Path(raw).expanduser()
    return config_file.parent / "state.json"


def load_state(path: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {"v": 2, "status": {}, "fail_push_at": {}}
    if not path.exists():
        return empty
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty
    if not isinstance(data, dict):
        return empty
    if data.get("v") == 2 and isinstance(data.get("status"), dict):
        out: dict[str, Any] = {
            "v": 2,
            "status": dict(data["status"]),
            "fail_push_at": dict(data.get("fail_push_at") or {}),
        }
        lw = data.get("last_weekly_status_week")
        if isinstance(lw, str):
            out["last_weekly_status_week"] = lw
        return out
    # Legacy: flach { "page:Name": "ok"|"fail", ... }
    status: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(v, str) and v in ("ok", "fail"):
            status[str(k)] = v
    return {"v": 2, "status": status, "fail_push_at": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=0)
    tmp.replace(path)


def send_pushover(
    user_key: str,
    api_token: str,
    title: str,
    message: str,
    priority: int = 0,
) -> None:
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": api_token,
                "user": user_key,
                "title": title[:250],
                "message": message[:1024],
                "priority": str(priority),
            },
        )
        r.raise_for_status()


def send_startup_ping(user_key: str, api_token: str) -> None:
    host = socket.gethostname()
    try:
        fqdn = socket.getfqdn()
    except OSError:
        fqdn = host
    when = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    msg = (
        f"WebpageWatcher ist gestartet.\n"
        f"Host: {host}\n"
        f"FQDN: {fqdn}\n"
        f"Zeit: {when}\n"
        f"Pushover-Verbindung funktioniert."
    )
    send_pushover(user_key, api_token, "WebpageWatcher · Start", msg)


def check_page(entry: dict[str, Any]) -> CheckResult:
    name = entry.get("name") or entry["url"]
    key = f"page:{name}"
    url = entry["url"]
    expect_status = int(entry.get("expect_status", 200))
    timeout = float(entry.get("timeout_seconds", 30))
    verify = entry.get("verify_tls", True)
    t = httpx.Timeout(timeout)
    try:
        with httpx.Client(timeout=t, verify=verify) as client:
            r = client.get(url, follow_redirects=True)
    except httpx.HTTPError as e:
        return CheckResult(name, key, False, f"Anfrage fehlgeschlagen: {e}")

    if r.status_code != expect_status:
        return CheckResult(
            name,
            key,
            False,
            f"HTTP {r.status_code}, erwartet {expect_status}",
        )

    if entry.get("expect_body_contains") is not None or entry.get(
        "expect_body_regex"
    ) is not None:
        ok, err = check_body_expectations(entry, r.text)
        if not ok:
            return CheckResult(name, key, False, err or "Inhalt ungültig")

    ok_a, err_a = check_anchor_expectations(entry, r.text)
    if not ok_a:
        return CheckResult(name, key, False, err_a or "Anker fehlt")

    return CheckResult(name, key, True, f"HTTP {r.status_code}")


def check_extra_http(entry: dict[str, Any]) -> CheckResult:
    name = entry.get("name") or entry["url"]
    key = f"extra:http:{name}"
    url = entry["url"]
    expect_status = int(entry.get("expect_status", 200))
    timeout = float(entry.get("timeout_seconds", 15))
    verify = entry.get("verify_tls", True)
    t = httpx.Timeout(timeout)
    try:
        with httpx.Client(timeout=t, verify=verify) as client:
            r = client.get(url, follow_redirects=True)
    except httpx.HTTPError as e:
        return CheckResult(
            name,
            key,
            False,
            f"Anfrage fehlgeschlagen: {e}",
            push_on_success=_push_on_success_flag(entry),
        )

    ok = r.status_code == expect_status
    detail = f"HTTP {r.status_code}" + (
        f", erwartet {expect_status}" if not ok else ""
    )
    return CheckResult(
        name,
        key,
        ok,
        detail,
        push_on_success=_push_on_success_flag(entry),
    )


def _push_on_success_flag(entry: dict[str, Any]) -> bool:
    if entry.get("push_on_success"):
        return True
    # notify_when: success = bei jedem erfolgreichen Lauf Push (je nach Intervall)
    return entry.get("notify_when") == "success"


def check_extra_ping(entry: dict[str, Any]) -> CheckResult:
    name = entry.get("name") or entry["host"]
    key = f"extra:ping:{name}"
    host = entry["host"]
    count = int(entry.get("count", 1))
    timeout_sec = int(entry.get("timeout_seconds", 5))

    try:
        proc = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout_sec), host],
            capture_output=True,
            text=True,
            timeout=timeout_sec * count + 10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return CheckResult(
            name,
            key,
            False,
            f"Ping nicht ausführbar/fehlgeschlagen: {e}",
            push_on_success=_push_on_success_flag(entry),
        )

    ok = proc.returncode == 0
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    last = tail[-1] if tail else "(keine Ausgabe)"
    return CheckResult(
        name,
        key,
        ok,
        last[:500],
        push_on_success=_push_on_success_flag(entry),
    )


def run_checks(config: dict[str, Any]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for p in config.get("pages") or []:
        nav_items = p.get("navigation") or []
        main = {k: v for k, v in p.items() if k != "navigation"}
        results.append(check_page(main))
        for nav in nav_items:
            results.append(check_page(merge_nav_entry(p, nav)))

    for e in config.get("extra_checks") or []:
        t = (e.get("type") or "http").lower()
        if t == "http":
            results.append(check_extra_http(e))
        elif t == "ping":
            results.append(check_extra_ping(e))
        else:
            name = e.get("name", t)
            results.append(
                CheckResult(
                    name,
                    f"extra:unknown:{name}",
                    False,
                    f"Unbekannter extra_checks type: {t}",
                )
            )
    return results


def _parse_iso_utc(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def decide_notifications(
    results: list[CheckResult],
    state: dict[str, Any],
    notify_cfg: dict[str, Any],
) -> tuple[list[tuple[str, str, int]], dict[str, Any]]:
    """
    Returns list of (title, message, priority) and new state blob (v2).
    """
    on_failure = notify_cfg.get("on_failure", True)
    on_recovery = notify_cfg.get("on_recovery", True)
    repeat_raw = notify_cfg.get("repeat_failure_reminder_minutes")
    if repeat_raw is None:
        repeat_mins = 180
    else:
        repeat_mins = max(0, int(repeat_raw))

    now = datetime.now(timezone.utc)
    prev = state.get("status") or {}
    fail_push_at: dict[str, str] = dict(state.get("fail_push_at") or {})
    new_status: dict[str, str] = dict(prev)
    out: list[tuple[str, str, int]] = []

    def mark_fail_push(key: str) -> None:
        fail_push_at[key] = now.isoformat()

    for r in results:
        cur = "ok" if r.ok else "fail"
        old = prev.get(r.key)
        new_status[r.key] = cur

        if cur == "ok":
            fail_push_at.pop(r.key, None)

        if old is None:
            if cur == "fail" and on_failure:
                out.append((f"Fehler: {r.name}", r.detail, 1))
                mark_fail_push(r.key)
            continue

        if old == "ok" and cur == "fail" and on_failure:
            out.append((f"Fehler: {r.name}", r.detail, 1))
            mark_fail_push(r.key)
        elif old == "fail" and cur == "ok" and on_recovery:
            out.append((f"OK wieder: {r.name}", r.detail, 0))
        elif (
            old == "fail"
            and cur == "fail"
            and on_failure
            and repeat_mins > 0
        ):
            last_s = fail_push_at.get(r.key)
            if last_s is None:
                # z. B. nach Upgrade: war schon fail, noch kein Zeitstempel → einmal melden
                out.append(
                    (f"Immer noch fehlgeschlagen: {r.name}", r.detail, 1)
                )
                mark_fail_push(r.key)
            else:
                try:
                    elapsed = now - _parse_iso_utc(last_s)
                    if elapsed >= timedelta(minutes=repeat_mins):
                        out.append(
                            (
                                f"Immer noch fehlgeschlagen: {r.name}",
                                r.detail,
                                1,
                            )
                        )
                        mark_fail_push(r.key)
                except (ValueError, TypeError):
                    mark_fail_push(r.key)

        if r.ok and r.push_on_success:
            out.append((f"OK: {r.name}", r.detail, 0))

    new_state: dict[str, Any] = {
        "v": 2,
        "status": new_status,
        "fail_push_at": fail_push_at,
    }
    lw = state.get("last_weekly_status_week")
    if isinstance(lw, str):
        new_state["last_weekly_status_week"] = lw
    return out, new_state


_WEEKDAY_NAMES: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _parse_weekday(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return int(v) % 7
    if isinstance(v, str):
        return _WEEKDAY_NAMES.get(v.strip().lower())
    return None


def _weekly_status_window_ok(
    now_tz: datetime,
    hour: int,
    minute: int,
    window_minutes: int,
) -> bool:
    start = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end = start + timedelta(minutes=max(1, window_minutes))
    return start <= now_tz < end


def format_weekly_status_message(results: list[CheckResult], now_tz: datetime) -> str:
    ok_n = sum(1 for r in results if r.ok)
    fail_n = len(results) - ok_n
    y, iso_week, _ = now_tz.isocalendar()
    tz_name = str(now_tz.tzinfo) if now_tz.tzinfo else ""
    lines = [
        f"Kalenderwoche {iso_week} ({y}), {now_tz.strftime('%d.%m.%Y %H:%M')}",
        f"Zeitzone: {tz_name}",
        f"Checks: {len(results)} · OK: {ok_n} · Fehler: {fail_n}",
    ]
    if fail_n == 0:
        lines.append("Alle Checks bestanden — Watcher läuft.")
    else:
        lines.append("Fehlgeschlagen:")
        for r in results:
            if not r.ok:
                d = (r.detail or "")[:120]
                lines.append(f"• {r.name}: {d}")
    return "\n".join(lines)[:1024]


def append_weekly_status_notification(
    messages: list[tuple[str, str, int]],
    new_state: dict[str, Any],
    results: list[CheckResult],
    notify_cfg: dict[str, Any],
) -> None:
    """Hängt ggf. eine Wochenstatus-Pushover-Meldung an (mutiert messages + new_state)."""
    ws = notify_cfg.get("weekly_status")
    if not isinstance(ws, dict) or not ws.get("enabled"):
        return
    wd = _parse_weekday(ws.get("weekday", "sunday"))
    if wd is None:
        return
    try:
        hour = int(ws.get("hour", 10))
        minute = int(ws.get("minute", 0))
    except (TypeError, ValueError):
        return
    window = int(ws.get("window_minutes", 15))
    window = max(1, min(window, 120))
    tz_name = str(ws.get("timezone") or "Europe/Zurich")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Zurich")
    now_tz = datetime.now(tz)
    if now_tz.weekday() != wd:
        return
    if not _weekly_status_window_ok(now_tz, hour, minute, window):
        return
    y, wk, _ = now_tz.isocalendar()
    week_tag = f"{y}-W{wk:02d}"
    if new_state.get("last_weekly_status_week") == week_tag:
        return
    body = format_weekly_status_message(results, now_tz)
    messages.append(("WebpageWatcher · Wochenstatus", body, -1))
    new_state["last_weekly_status_week"] = week_tag


def main() -> int:
    parser = argparse.ArgumentParser(description="Webpage / NUK monitoring")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config.yaml",
        help="Pfad zu config.yaml",
    )
    parser.add_argument(
        "--startup-ping",
        action="store_true",
        help="Nur eine Pushover-Testnachricht senden und beenden (keine Seiten-Checks).",
    )
    args = parser.parse_args()
    config_path: Path = args.config

    if not config_path.exists():
        print(
            f"Konfiguration fehlt: {config_path}\n"
            f"Kopieren Sie config.example.yaml nach config.yaml "
            f"(pages.yaml kommt aus dem Repo).",
            file=sys.stderr,
        )
        return 2

    config = load_config(config_path)
    po = config.get("pushover") or {}
    user_key = os.environ.get("PUSHOVER_USER_KEY") or po.get("user_key")
    api_token = os.environ.get("PUSHOVER_API_TOKEN") or po.get("api_token")

    if not user_key or not api_token:
        print(
            "Pushover user_key und api_token in config.yaml oder per Umgebungsvariable setzen.",
            file=sys.stderr,
        )
        return 2

    if args.startup_ping:
        try:
            send_startup_ping(user_key, api_token)
        except httpx.HTTPError as e:
            print(f"Pushover (Startup): {e}", file=sys.stderr)
            return 1
        print("Startup-Pushover gesendet.")
        return 0

    try:
        merge_pages_from_file(config, config_path)
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2

    state_file = state_path_from_config(config, config_path)
    state = load_state(state_file)
    notify_cfg = config.get("notify") or {}

    if notify_cfg.get("on_startup"):
        try:
            send_startup_ping(user_key, api_token)
        except httpx.HTTPError as e:
            print(f"Pushover (Startup): {e}", file=sys.stderr)

    results = run_checks(config)
    messages, new_state = decide_notifications(results, state, notify_cfg)
    append_weekly_status_notification(messages, new_state, results, notify_cfg)
    save_state(state_file, new_state)

    for title, message, priority in messages:
        try:
            send_pushover(user_key, api_token, title, message, priority=priority)
        except httpx.HTTPError as e:
            print(f"Pushover fehlgeschlagen: {e}", file=sys.stderr)

    any_fail = any(not r.ok for r in results)
    for r in results:
        status = "OK " if r.ok else "FEHLER"
        print(f"[{status}] {r.name}: {r.detail}")

    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
