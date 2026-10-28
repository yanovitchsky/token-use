from __future__ import annotations

from datetime import datetime, timezone


def parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def to_iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def unix_to_dt(seconds: int | float) -> datetime:
    return datetime.fromtimestamp(float(seconds), tz=timezone.utc)


def human_reset(resets_at: datetime | None, now: datetime | None = None) -> str | None:
    if resets_at is None:
        return None
    if now is None:
        now = datetime.now().astimezone()
    local = resets_at.astimezone(now.tzinfo)
    stamp = local.strftime("%a %-d %b %H:%M")
    delta = local - now
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return f"reset {stamp}"
    minutes = seconds // 60
    days, minutes = divmod(minutes, 60 * 24)
    hours, minutes = divmod(minutes, 60)
    if days:
        rel = f"{days}d {hours}h"
    else:
        rel = f"{hours}h {minutes}m"
    return f"resets in {rel} ({stamp})"


def window_names(event: datetime, now: datetime) -> list[str]:
    local_event = event.astimezone(now.tzinfo)
    local_now = now.astimezone(now.tzinfo)
    if local_event > local_now:
        return []
    names: list[str] = []
    if local_event.date() == local_now.date():
        names.append("today")
    age = local_now - local_event
    if age.total_seconds() <= 7 * 24 * 3600:
        names.append("7d")
    if age.total_seconds() <= 30 * 24 * 3600:
        names.append("30d")
    return names
