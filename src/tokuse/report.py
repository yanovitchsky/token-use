from __future__ import annotations

from datetime import datetime, timezone

from tokuse import claude_usage, codex_usage, cursor_usage
from tokuse.timefmt import human_reset, parse_iso

TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")
WINDOW_ORDER = ("today", "7d", "30d", "billing")
WINDOW_LABELS = {
    "today": "Today",
    "7d": "7 days",
    "30d": "30 days",
    "billing": "Billing cycle",
}
PROVIDER_ORDER = ("cursor", "codex", "claude")
PROVIDER_TITLES = {"cursor": "Cursor", "codex": "Codex", "claude": "Claude"}
MENU_LABELS = {"cursor": "C", "codex": "X", "claude": "A"}


def empty_tokens() -> dict:
    return {key: 0 for key in TOKEN_KEYS}


def add_tokens(dst: dict, src: dict) -> None:
    for key in TOKEN_KEYS:
        dst[key] += int(src.get(key) or 0)


def row_total(row: dict) -> int:
    return sum(int(row.get(key) or 0) for key in TOKEN_KEYS)


def finalize_models(buckets: dict[str, dict]) -> list[dict]:
    rows = []
    for model, data in buckets.items():
        row = {"model": model, **data["tokens"]}
        cents = data["cents"]
        row["cents"] = None if cents is None else float(cents)
        rows.append(row)
    rows.sort(key=lambda row: (-row_total(row), row["model"]))
    return rows


def build(now: datetime | None = None) -> dict:
    if now is None:
        now = datetime.now().astimezone()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "providers": {
            "cursor": cursor_usage.collect(),
            "codex": codex_usage.collect(now),
            "claude": claude_usage.collect(now),
        },
    }


def _pct(reset: dict | None) -> str:
    if not reset or reset.get("used_percent") is None:
        return "—"
    return f"{int(round(float(reset['used_percent'])))}%"


def _find_reset(provider: dict, name: str) -> dict | None:
    for reset in provider.get("resets") or []:
        if reset.get("name") == name:
            return reset
    return None


def menu_percents(report: dict) -> dict[str, str]:
    providers = report["providers"]
    cursor = providers["cursor"]
    codex = providers["codex"]
    claude = providers["claude"]
    cursor_reset = _find_reset(cursor, "Billing cycle") if cursor.get("ok") else None
    if codex.get("ok"):
        codex_reset = _find_reset(codex, "5h") or (codex.get("resets") or [None])[0]
    else:
        codex_reset = None
    claude_reset = _find_reset(claude, "5h") if claude.get("ok") else None
    return {
        "cursor": _pct(cursor_reset),
        "codex": _pct(codex_reset),
        "claude": _pct(claude_reset),
    }


def _reset_phrase(reset: dict, now: datetime) -> str:
    when = reset.get("resets_at")
    dt = parse_iso(when) if isinstance(when, str) else None
    text = human_reset(dt, now) if dt else None
    if text:
        return text
    if reset.get("detail"):
        return str(reset["detail"])
    return "—"


def format_menu(report: dict, now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now().astimezone()
    percents = menu_percents(report)
    title = " · ".join(f"{MENU_LABELS[name]} {percents[name]}" for name in PROVIDER_ORDER)
    lines = [title, "---"]
    for name in PROVIDER_ORDER:
        provider = report["providers"][name]
        title_name = PROVIDER_TITLES[name]
        if not provider.get("ok"):
            lines.append(f"{title_name}: — · {provider.get('error') or 'unavailable'}")
            continue
        resets = provider.get("resets") or []
        if not resets:
            lines.append(f"{title_name}: —")
            continue
        for reset in resets:
            lines.append(
                f"{title_name} {reset.get('name')}: {_pct(reset)} · {_reset_phrase(reset, now)}"
            )
    lines.append("---")
    lines.append("Full report")
    return "\n".join(lines) + "\n"


def _fmt_int(value: int) -> str:
    return f"{int(value):,}"


def _fmt_pct(reset: dict) -> str:
    if reset.get("used_percent") is None:
        pct = "—"
    else:
        pct = f"{float(reset['used_percent']):.0f}%"
    return pct


def format_human(report: dict, now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now().astimezone()
    blocks: list[str] = []
    for name in PROVIDER_ORDER:
        provider = report["providers"][name]
        lines = [PROVIDER_TITLES[name]]
        if not provider.get("ok"):
            lines.append(provider.get("error") or "unavailable")
            blocks.append("\n".join(lines))
            continue
        resets = provider.get("resets") or []
        if not resets:
            lines.append("No reset data")
        for reset in resets:
            phrase = _reset_phrase(reset, now)
            line = f"{reset.get('name')}: {_fmt_pct(reset)} · {phrase}"
            detail = reset.get("detail")
            if detail and detail not in phrase:
                line += f" · {detail}"
            lines.append(line)
        windows = provider.get("windows") or {}
        for key in WINDOW_ORDER:
            if key not in windows:
                continue
            window = windows[key]
            rows = window.get("by_model") or []
            totals = window.get("totals") or empty_tokens()
            show_usd = any(row.get("cents") is not None for row in rows)
            lines.append("")
            lines.append(window.get("label") or WINDOW_LABELS[key])
            header = ["Model", "Input", "Output", "Cache read", "Cache write", "Reasoning"]
            if show_usd:
                header.append("USD")
            table = [header]
            for row in rows:
                cells = [
                    str(row.get("model") or "unknown"),
                    _fmt_int(row.get("input") or 0),
                    _fmt_int(row.get("output") or 0),
                    _fmt_int(row.get("cache_read") or 0),
                    _fmt_int(row.get("cache_write") or 0),
                    _fmt_int(row.get("reasoning") or 0),
                ]
                if show_usd:
                    cents = row.get("cents")
                    cells.append("—" if cents is None else f"${cents / 100:.2f}")
                table.append(cells)
            total_cells = [
                "Total",
                _fmt_int(totals.get("input") or 0),
                _fmt_int(totals.get("output") or 0),
                _fmt_int(totals.get("cache_read") or 0),
                _fmt_int(totals.get("cache_write") or 0),
                _fmt_int(totals.get("reasoning") or 0),
            ]
            if show_usd:
                cents_values = [row.get("cents") for row in rows if row.get("cents") is not None]
                total_cells.append(f"${sum(cents_values) / 100:.2f}" if cents_values else "—")
            table.append(total_cells)
            widths = [max(len(row[i]) for row in table) for i in range(len(header))]
            for index, row in enumerate(table):
                lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
                if index == 0:
                    lines.append("  ".join("-" * widths[i] for i in range(len(header))))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
