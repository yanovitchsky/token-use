from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from tokuse.timefmt import parse_iso, to_iso_utc, unix_to_dt, window_names

ZERO_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "cache_write_input_tokens",
)


def _home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _session_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for folder in ("sessions", "archived_sessions"):
        base = root / folder
        if not base.exists():
            continue
        for path in base.rglob("*.jsonl"):
            if path.name.endswith(".jsonl.zst"):
                continue
            files.append(path)
    return files


def _dir_midnight(path: Path) -> datetime | None:
    parts = path.parts
    for index in range(len(parts) - 3):
        year, month, day = parts[index : index + 3]
        if len(year) == 4 and year.isdigit() and month.isdigit() and day.isdigit():
            local = datetime.now().astimezone().tzinfo
            return datetime(int(year), int(month), int(day), tzinfo=local)
    return None


def _map_usage(info: dict) -> dict:
    cached = int(info.get("cached_input_tokens") or 0)
    inp = int(info.get("input_tokens") or 0)
    reasoning = int(info.get("reasoning_output_tokens") or 0)
    output = int(info.get("output_tokens") or 0)
    return {
        "input": max(0, inp - cached),
        "output": max(0, output - reasoning),
        "cache_read": cached,
        "cache_write": int(info.get("cache_write_input_tokens") or 0),
        "reasoning": reasoning,
    }


def _all_zero(info: dict) -> bool:
    return all(int(info.get(field) or 0) == 0 for field in ZERO_FIELDS)


def _window_label(kind: str, minutes) -> str:
    if minutes is None:
        return "5h" if kind == "primary" else "Week"
    minutes = int(minutes)
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "Week"
    if minutes >= 40_000:
        return "Month"
    if minutes % 1440 == 0 and minutes >= 1440:
        return f"{minutes // 1440}d"
    return f"{minutes}m"


def _label_from_seconds(seconds) -> str:
    if seconds is None:
        return "5h"
    return _window_label("primary", int(seconds) // 60)


def _reset_from_limit(name_kind: str, payload: dict | None) -> dict | None:
    if not payload:
        return None
    resets_at = payload.get("resets_at")
    iso = to_iso_utc(unix_to_dt(resets_at)) if resets_at is not None else None
    percent = payload.get("used_percent")
    return {
        "name": _window_label(name_kind, payload.get("window_minutes")),
        "used_percent": None if percent is None else float(percent),
        "resets_at": iso,
        "detail": None,
    }


def _rate_limits(files: list[Path]) -> list[dict]:
    ordered = sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)
    for path in ordered:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "event_msg":
                continue
            payload = record.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            limits = payload.get("rate_limits")
            if not limits:
                continue
            resets = []
            primary = _reset_from_limit("primary", limits.get("primary"))
            secondary = _reset_from_limit("secondary", limits.get("secondary"))
            if primary:
                resets.append(primary)
            if secondary:
                resets.append(secondary)
            credits = limits.get("credits")
            if credits and credits.get("unlimited") is False:
                balance = credits.get("balance")
                resets.append(
                    {
                        "name": "Credits",
                        "used_percent": None,
                        "resets_at": None,
                        "detail": None if balance is None else str(balance),
                    }
                )
            return resets
    return []


def resets_from_wham(body: dict) -> list[dict]:
    limit = body.get("rate_limit") or {}
    resets = []
    for key, fallback in (("primary_window", "5h"), ("secondary_window", "Week")):
        window = limit.get(key)
        if not isinstance(window, dict):
            continue
        reset_at = window.get("reset_at")
        percent = window.get("used_percent")
        resets.append(
            {
                "name": _label_from_seconds(window.get("limit_window_seconds")) if window.get("limit_window_seconds") is not None else fallback,
                "used_percent": None if percent is None else float(percent),
                "resets_at": to_iso_utc(unix_to_dt(reset_at)) if reset_at is not None else None,
                "detail": None,
            }
        )
    credits = body.get("credits")
    if isinstance(credits, dict) and credits.get("unlimited") is False and credits.get("has_credits"):
        balance = credits.get("balance")
        resets.append(
            {
                "name": "Credits",
                "used_percent": None,
                "resets_at": None,
                "detail": None if balance is None else str(balance),
            }
        )
    return resets


def _jwt_exp(token: str) -> int | None:
    try:
        import base64

        payload = token.split(".")[1]
        pad = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
        return None
    exp = data.get("exp")
    return int(exp) if exp is not None else None


def _save_auth(path: Path, auth: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(auth))
    tmp.replace(path)


def _refresh_codex(auth: dict, path: Path) -> dict:
    tokens = auth.get("tokens") or {}
    refresh = tokens.get("refresh_token")
    if not refresh:
        return auth
    client_id = "app_EMoamEEZ73f0CkXaXp7hrann"
    access = tokens.get("access_token") or ""
    exp = _jwt_exp(access) if access else None
    now = int(datetime.now(timezone.utc).timestamp())
    if exp is not None and exp - now > 60:
        return auth
    form = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh,
        }
    ).encode()
    req = urllib.request.Request(
        "https://auth.openai.com/oauth/token",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded", "accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return auth
    if not isinstance(payload, dict) or not payload.get("access_token"):
        return auth
    updated = dict(auth)
    new_tokens = dict(tokens)
    new_tokens["access_token"] = payload["access_token"]
    if payload.get("refresh_token"):
        new_tokens["refresh_token"] = payload["refresh_token"]
    if payload.get("id_token"):
        new_tokens["id_token"] = payload["id_token"]
    updated["tokens"] = new_tokens
    updated["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        _save_auth(path, updated)
    except OSError:
        return updated
    return updated


def _live_resets(root: Path) -> list[dict] | None:
    if os.environ.get("CODEX_HOME"):
        path = root / "auth.json"
    else:
        path = Path.home() / ".codex" / "auth.json"
    if not path.exists():
        return None
    try:
        auth = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    auth = _refresh_codex(auth, path)
    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token")
    account = tokens.get("account_id")
    if not access:
        return None
    headers = {"Authorization": f"Bearer {access}", "accept": "application/json"}
    if account:
        headers["chatgpt-account-id"] = str(account)
    req = urllib.request.Request("https://chatgpt.com/backend-api/wham/usage", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            return None
        auth = _refresh_codex({**auth, "tokens": {**tokens, "access_token": ""}}, path)
        access = (auth.get("tokens") or {}).get("access_token")
        if not access:
            return None
        headers["Authorization"] = f"Bearer {access}"
        try:
            with urllib.request.urlopen(
                urllib.request.Request("https://chatgpt.com/backend-api/wham/usage", headers=headers),
                timeout=15,
            ) as resp:
                body = json.loads(resp.read().decode())
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
            return None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None
    if not isinstance(body, dict):
        return None
    resets = resets_from_wham(body)
    return resets or None


def _new_bucket() -> dict[str, dict]:
    return {}


def _add(bucket: dict[str, dict], model: str, tokens: dict) -> None:
    from tokuse.report import add_tokens, empty_tokens

    slot = bucket.setdefault(model, {"tokens": empty_tokens(), "cents": None})
    add_tokens(slot["tokens"], tokens)


def collect(now: datetime | None = None) -> dict:
    try:
        return _collect(now)
    except Exception:
        return {"ok": False, "error": "Codex sessions could not be read", "resets": [], "windows": {}}


def _collect(now: datetime | None = None) -> dict:
    if now is None:
        now = datetime.now().astimezone()
    root = _home()
    files = _session_files(root)
    buckets = {key: _new_bucket() for key in ("today", "7d", "30d")}
    if not files and not (root / "sessions").exists() and not (root / "archived_sessions").exists():
        return {"ok": True, "error": None, "resets": [], "windows": {}}
    for path in files:
        model = "unknown"
        try:
            handle = path.open(errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                outer = record.get("type")
                payload = record.get("payload") or {}
                if outer in {"turn_context", "session_meta"}:
                    found = payload.get("model")
                    if isinstance(found, str) and found:
                        model = found
                    continue
                if outer != "event_msg" or payload.get("type") != "token_count":
                    continue
                info = (payload.get("info") or {}).get("last_token_usage")
                if not isinstance(info, dict) or _all_zero(info):
                    continue
                stamp = record.get("timestamp")
                if stamp:
                    event = parse_iso(str(stamp))
                else:
                    event = _dir_midnight(path)
                    if event is None:
                        continue
                tokens = _map_usage(info)
                for name in window_names(event, now):
                    _add(buckets[name], model, tokens)
    windows = {}
    for name, bucket in buckets.items():
        if not bucket:
            continue
        from tokuse.report import add_tokens, empty_tokens, finalize_models

        rows = finalize_models(bucket)
        totals = empty_tokens()
        for row in rows:
            add_tokens(totals, row)
        label = {"today": "Today", "7d": "7 days", "30d": "30 days"}[name]
        windows[name] = {"label": label, "by_model": rows, "totals": totals}
    live = _live_resets(root)
    return {"ok": True, "error": None, "resets": live if live is not None else _rate_limits(files), "windows": windows}
