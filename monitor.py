#!/usr/bin/env python3
"""
Webpage / connectivity monitoring with Pushover alerts.
Designed for hourly runs via systemd timer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def state_path_from_config(config: dict[str, Any], config_file: Path) -> Path:
    env_state = os.environ.get("WEBPAGE_WATCHER_STATE_FILE")
    if env_state:
        return Path(env_state).expanduser()
    raw = config.get("state_file")
    if raw:
        return Path(raw).expanduser()
    return config_file.parent / "state.json"


def load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path: Path, state: dict[str, str]) -> None:
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
    # notify_when: success = bei jedem erfolgreichen Lauf Push (stündlich)
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
        results.append(check_page(p))

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


def decide_notifications(
    results: list[CheckResult],
    prev: dict[str, str],
    notify_cfg: dict[str, Any],
) -> tuple[list[tuple[str, str, int]], dict[str, str]]:
    """
    Returns list of (title, message, priority) and new state map.
    """
    on_failure = notify_cfg.get("on_failure", True)
    on_recovery = notify_cfg.get("on_recovery", True)
    out: list[tuple[str, str, int]] = []
    new_state: dict[str, str] = dict(prev)

    for r in results:
        cur = "ok" if r.ok else "fail"
        old = prev.get(r.key)
        new_state[r.key] = cur

        if old is None:
            # Erster Lauf: keine Benachrichtigung, Zustand nur speichern
            continue

        if old == "ok" and cur == "fail" and on_failure:
            out.append(
                (
                    f"Fehler: {r.name}",
                    r.detail,
                    1,
                )
            )
        elif old == "fail" and cur == "ok" and on_recovery:
            out.append(
                (
                    f"OK wieder: {r.name}",
                    r.detail,
                    0,
                )
            )

        if r.ok and r.push_on_success:
            out.append(
                (
                    f"OK: {r.name}",
                    r.detail,
                    0,
                )
            )

    return out, new_state


def main() -> int:
    parser = argparse.ArgumentParser(description="Webpage / NUK monitoring")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config.yaml",
        help="Pfad zu config.yaml",
    )
    args = parser.parse_args()
    config_path: Path = args.config

    if not config_path.exists():
        print(
            f"Konfiguration fehlt: {config_path}\n"
            f"Kopieren Sie config.example.yaml nach config.yaml "
            f"und pages.example.yaml nach pages.yaml.",
            file=sys.stderr,
        )
        return 2

    config = load_config(config_path)
    try:
        merge_pages_from_file(config, config_path)
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    po = config.get("pushover") or {}
    user_key = os.environ.get("PUSHOVER_USER_KEY") or po.get("user_key")
    api_token = os.environ.get("PUSHOVER_API_TOKEN") or po.get("api_token")

    if not user_key or not api_token:
        print(
            "Pushover user_key und api_token in config.yaml oder per Umgebungsvariable setzen.",
            file=sys.stderr,
        )
        return 2

    state_file = state_path_from_config(config, config_path)
    prev = load_state(state_file)
    notify_cfg = config.get("notify") or {}

    results = run_checks(config)
    messages, new_state = decide_notifications(results, prev, notify_cfg)
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
