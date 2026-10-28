from __future__ import annotations

import base64
import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from tokuse.timefmt import to_iso_utc, unix_to_dt

CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"
API = "https://api2.cursor.sh"
TIMEOUT = 15


def _int(value) -> int:
    if value is None or value is False:
        return 0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        return int(float(text)) if "." in text else int(text)
    return int(value)


def _state_db() -> Path:
    return Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"


def _read_auth() -> tuple[str | None, str | None]:
    path = _state_db()
    if not path.exists():
        return None, None
    uri = "file:" + quote(str(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = dict(
            conn.execute(
                "SELECT key, value FROM ItemTable WHERE key IN (?, ?)",
                ("cursorAuth/accessToken", "cursorAuth/refreshToken"),
            ).fetchall()
        )
    finally:
        conn.close()
    access = rows.get("cursorAuth/accessToken")
    refresh = rows.get("cursorAuth/refreshToken")
    if isinstance(access, bytes):
        access = access.decode()
    if isinstance(refresh, bytes):
        refresh = refresh.decode()
    return access or None, refresh or None


def _jwt_exp(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        pad = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
        return None
    exp = data.get("exp")
    return int(exp) if exp is not None else None


def _post(path: str, body: dict, token: str | None) -> tuple[int, dict | None, bytes]:
    data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    if not raw:
        return status, None, raw
    try:
        parsed = json.loads(raw.decode())
    except json.JSONDecodeError:
        return status, None, raw
    return status, parsed if isinstance(parsed, dict) else None, raw


def _refresh_cursor(refresh: str) -> str | None:
    status, body, _raw = _post(
        "/oauth/token",
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh,
        },
        None,
    )
    if status == 200 and body and body.get("access_token"):
        return str(body["access_token"])
    return None


def _access_token(stored: str, refresh: str | None) -> tuple[str | None, str | None]:
    exp = _jwt_exp(stored)
    now = int(datetime.now(timezone.utc).timestamp())
    token = stored
    if refresh and exp is not None and exp - now <= 60:
        refreshed = _refresh_cursor(refresh)
        if refreshed:
            token = refreshed
    return token, None


def _ms_to_iso(value) -> str | None:
    if value is None:
        return None
    number = int(value)
    if number > 10_000_000_000:
        dt = datetime.fromtimestamp(number / 1000, tz=timezone.utc)
    else:
        dt = unix_to_dt(number)
    return to_iso_utc(dt)


def _period_bounds(period: dict) -> str | None:
    start = _ms_to_iso(period.get("billingCycleStart"))
    end = _ms_to_iso(period.get("billingCycleEnd"))
    if not start or not end:
        return None
    from tokuse.timefmt import parse_iso

    def fmt(value: str) -> str:
        return parse_iso(value).astimezone().strftime("%-d %b %Y")

    return f"{fmt(start)} – {fmt(end)}"


def _billing_label(period: dict) -> str:
    bounds = _period_bounds(period)
    if not bounds:
        return "Billing cycle"
    return f"Billing cycle ({bounds})"


def _spend_detail(payload: dict, plan_usage: dict) -> str | None:
    spend = plan_usage.get("includedSpend")
    limit = plan_usage.get("limit")
    if spend is None or limit is None:
        plan = ((payload.get("individualUsage") or {}).get("plan") or {})
        spend = plan.get("used")
        limit = plan.get("limit")
    if spend is None or limit is None:
        return None
    return f"included ${int(spend) / 100:.2f} / ${int(limit) / 100:.2f}"


def _resets(period: dict) -> list[dict]:
    plan_usage = period.get("planUsage") or {}
    resets_at = _ms_to_iso(period.get("billingCycleEnd"))
    parts = [part for part in (_period_bounds(period), _spend_detail(period, plan_usage)) if part]
    detail = " · ".join(parts) if parts else None
    specs = (
        ("Billing cycle", plan_usage.get("totalPercentUsed"), detail),
        ("Auto", plan_usage.get("autoPercentUsed"), None),
        ("API", plan_usage.get("apiPercentUsed"), None),
    )
    resets = []
    for name, percent, extra in specs:
        if percent is None:
            continue
        resets.append(
            {
                "name": name,
                "used_percent": float(percent),
                "resets_at": resets_at,
                "detail": extra,
            }
        )
    return resets


def _window_from_aggregated(body: dict | None, label: str = "Billing cycle") -> dict:
    from tokuse.report import empty_tokens

    rows = []
    aggregations = (body or {}).get("aggregations") or []
    for item in aggregations:
        model = item.get("modelIntent") or "unknown"
        rows.append(
            {
                "model": model,
                "input": _int(item.get("inputTokens")),
                "output": _int(item.get("outputTokens")),
                "cache_read": _int(item.get("cacheReadTokens")),
                "cache_write": _int(item.get("cacheWriteTokens")),
                "reasoning": 0,
                "cents": None if item.get("totalCents") is None else float(item.get("totalCents")),
            }
        )
    rows.sort(key=lambda row: (-(row["input"] + row["output"] + row["cache_read"] + row["cache_write"]), row["model"]))
    totals = empty_tokens()
    total_map = {
        "input": body.get("totalInputTokens") if body else None,
        "output": body.get("totalOutputTokens") if body else None,
        "cache_read": body.get("totalCacheReadTokens") if body else None,
        "cache_write": body.get("totalCacheWriteTokens") if body else None,
    }
    if body and any(value is not None for value in total_map.values()):
        for key, value in total_map.items():
            totals[key] = _int(value)
    else:
        for row in rows:
            for key in totals:
                totals[key] += row[key]
    return {"label": label, "by_model": rows, "totals": totals}


def _failed(message: str) -> dict:
    return {"ok": False, "error": message, "resets": [], "windows": {}}


def _from_bodies(period: dict, aggregated: dict | None, aggregated_error: str | None) -> dict:
    from tokuse.report import empty_tokens

    resets = _resets(period)
    label = _billing_label(period)
    if aggregated_error:
        return {
            "ok": True,
            "error": aggregated_error,
            "resets": resets,
            "windows": {
                "billing": {"label": label, "by_model": [], "totals": empty_tokens()}
            },
        }
    return {
        "ok": True,
        "error": None,
        "resets": resets,
        "windows": {"billing": _window_from_aggregated(aggregated, label)},
    }


def _load_fixture(directory: Path) -> dict:
    period = json.loads((directory / "period.json").read_text())
    _plan = json.loads((directory / "plan.json").read_text())
    if "planUsage" not in period and "planUsage" in _plan:
        period = _plan
    aggregated_path = directory / "aggregated.json"
    raw = aggregated_path.read_text().strip()
    if not raw:
        return _from_bodies(period, None, "Cursor token breakdown unavailable")
    aggregated = json.loads(raw)
    return _from_bodies(period, aggregated, None)


def collect() -> dict:
    try:
        return _collect()
    except Exception:
        return _failed("Cursor request failed")


def _collect() -> dict:
    fixture = os.environ.get("TOKUSE_CURSOR_FIXTURE")
    if fixture:
        return _load_fixture(Path(fixture))
    access, refresh = _read_auth()
    if not access:
        return _failed("Cursor is not signed in")
    token, _err = _access_token(access, refresh)
    if not token:
        return _failed("Cursor is not signed in")
    status, period, _raw = _post("/aiserver.v1.DashboardService/GetCurrentPeriodUsage", {}, token)
    if status == 401 and refresh:
        refreshed = _refresh_cursor(refresh)
        if refreshed:
            token = refreshed
            status, period, _raw = _post("/aiserver.v1.DashboardService/GetCurrentPeriodUsage", {}, token)
    if status == 401:
        return _failed("Cursor session expired — open Cursor and sign in again")
    if status != 200 or not period:
        return _failed("Cursor request failed")
    _status, plan, _raw = _post("/aiserver.v1.DashboardService/GetPlanInfo", {}, token)
    if plan and "planUsage" not in period and "planUsage" in plan:
        period = {**plan, **period}
    end = period.get("billingCycleEnd")
    start = period.get("billingCycleStart")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = {"teamId": -1, "startDate": int(start) if start is not None else 0, "endDate": now_ms if end is None else int(end)}
    # endDate is now, startDate is billing cycle start. Plan: endDate now ms.
    body["endDate"] = now_ms
    if start is not None:
        body["startDate"] = int(start)
    agg_status, aggregated, raw = _post(
        "/aiserver.v1.DashboardService/GetAggregatedUsageEvents",
        body,
        token,
    )
    if agg_status == 404 or not raw:
        return _from_bodies(period, None, "Cursor token breakdown unavailable")
    if agg_status != 200 or aggregated is None:
        return _from_bodies(period, None, "Cursor token breakdown unavailable")
    return _from_bodies(period, aggregated, None)
