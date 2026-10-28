from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from tokuse.timefmt import parse_iso, window_names

TIMEOUT = 15
KEYCHAIN_SERVICE = "Claude Code-credentials"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
RESET_KEYS = (
    ("five_hour", "5h"),
    ("seven_day", "Week"),
    ("seven_day_opus", "Week Opus"),
    ("seven_day_sonnet", "Week Sonnet"),
)


def _claude_dir() -> Path:
    override = os.environ.get("TOKUSE_CLAUDE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def _login_reset() -> dict:
    return {
        "name": "Quota",
        "used_percent": None,
        "resets_at": None,
        "detail": "claude login required",
    }


def _map_resets(body: dict) -> list[dict]:
    resets = []
    for key, name in RESET_KEYS:
        item = body.get(key)
        if not item:
            continue
        percent = item.get("utilization")
        resets.append(
            {
                "name": name,
                "used_percent": None if percent is None else float(percent),
                "resets_at": item.get("resets_at"),
                "detail": None,
            }
        )
    return resets


def _keychain_credentials() -> dict | None:
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _write_keychain(data: dict) -> None:
    try:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-a",
                os.environ.get("USER") or "tokuse",
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
                json.dumps(data),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return


def _oauth(data: dict | None) -> dict:
    if not isinstance(data, dict):
        return {}
    oauth = data.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else {}


def _access_from(data: dict | None) -> str:
    return str(_oauth(data).get("accessToken") or "")


def _needs_refresh(data: dict | None) -> bool:
    expires = _oauth(data).get("expiresAt")
    if expires is None:
        return False
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    return float(expires) - now_ms <= 60_000


def _refresh(data: dict, persist: bool) -> dict:
    refresh = str(_oauth(data).get("refreshToken") or "")
    if not refresh:
        return data
    body = json.dumps(
        {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": CLIENT_ID}
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return data
    access = payload.get("access_token") if isinstance(payload, dict) else None
    if not access:
        return data
    oauth = dict(_oauth(data))
    oauth["accessToken"] = access
    if payload.get("refresh_token"):
        oauth["refreshToken"] = payload["refresh_token"]
    if payload.get("expires_in"):
        oauth["expiresAt"] = int(datetime.now(timezone.utc).timestamp() * 1000) + int(payload["expires_in"]) * 1000
    updated = dict(data)
    updated["claudeAiOauth"] = oauth
    if persist:
        _write_keychain(updated)
    return updated


def _usage_request(token: str) -> dict | None:
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"_unauthorized": True}
        return None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None
    return body if isinstance(body, dict) else None


def _load_credentials(directory: Path) -> tuple[dict | None, bool]:
    if os.environ.get("TOKUSE_CLAUDE_DIR"):
        path = directory / ".credentials.json"
        if not path.exists():
            return None, False
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None, False
        return (data if isinstance(data, dict) else None), False
    data = _keychain_credentials()
    if data and _access_from(data):
        return data, True
    path = directory / ".credentials.json"
    if not path.exists():
        return None, False
    try:
        file_data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, False
    return (file_data if isinstance(file_data, dict) else None), False


def _fetch_usage(directory: Path) -> list[dict]:
    fixture = os.environ.get("TOKUSE_CLAUDE_USAGE_FIXTURE")
    if fixture:
        return _map_resets(json.loads(Path(fixture).read_text()))
    data, persist = _load_credentials(directory)
    if not data or not _access_from(data):
        return [_login_reset()]
    if _needs_refresh(data):
        data = _refresh(data, persist)
    body = _usage_request(_access_from(data))
    if isinstance(body, dict) and body.get("_unauthorized") and _oauth(data).get("refreshToken"):
        data = _refresh(data, persist)
        body = _usage_request(_access_from(data))
    if not isinstance(body, dict) or body.get("_unauthorized"):
        return [_login_reset()]
    return _map_resets(body)


def _usage_tokens(usage: dict) -> dict:
    return {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
        "reasoning": 0,
    }


def collect(now: datetime | None = None) -> dict:
    try:
        return _collect(now)
    except Exception:
        return {"ok": False, "error": "Claude transcripts could not be read", "resets": [], "windows": {}}


def _collect(now: datetime | None = None) -> dict:
    if now is None:
        now = datetime.now().astimezone()
    directory = _claude_dir()
    buckets: dict[str, dict] = {"today": {}, "7d": {}, "30d": {}}
    projects = directory / "projects"
    seen: set[str] = set()
    if projects.exists():
        for path in projects.rglob("*.jsonl"):
            try:
                lines = path.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "assistant":
                    continue
                message = record.get("message") or {}
                model = message.get("model")
                if not model or model == "<synthetic>":
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                message_id = message.get("id")
                if message_id:
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                stamp = record.get("timestamp")
                if not stamp:
                    continue
                event = parse_iso(str(stamp))
                tokens = _usage_tokens(usage)
                from tokuse.report import add_tokens, empty_tokens

                for name in window_names(event, now):
                    slot = buckets[name].setdefault(model, {"tokens": empty_tokens(), "cents": None})
                    add_tokens(slot["tokens"], tokens)
    windows = {}
    labels = {"today": "Today", "7d": "7 days", "30d": "30 days"}
    for name, bucket in buckets.items():
        if not bucket:
            continue
        from tokuse.report import add_tokens, empty_tokens, finalize_models

        rows = finalize_models(bucket)
        totals = empty_tokens()
        for row in rows:
            add_tokens(totals, row)
        windows[name] = {"label": labels[name], "by_model": rows, "totals": totals}
    return {"ok": True, "error": None, "resets": _fetch_usage(directory), "windows": windows}
