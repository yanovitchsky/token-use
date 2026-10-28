import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokuse.cli import main
from tokuse.report import build, format_menu


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_codex_totals_and_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    stamp = now.isoformat().replace("+00:00", "Z")
    event = {
        "type": "event_msg",
        "timestamp": stamp,
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 40,
                    "output_tokens": 10,
                    "reasoning_output_tokens": 4,
                    "cache_write_input_tokens": 0,
                }
            },
        },
    }
    later = json.loads(json.dumps(event))
    later["payload"]["rate_limits"] = {
        "primary": {"used_percent": 25, "window_minutes": 300, "resets_at": 1_800_000_000},
        "secondary": {"used_percent": 10, "window_minutes": 10080, "resets_at": 1_800_000_000},
    }
    _write_jsonl(tmp_path / "sessions" / "2026" / "10" / "19" / "rollout-a.jsonl", [event])
    archived = tmp_path / "archived_sessions" / "2026" / "10" / "19" / "rollout-b.jsonl"
    _write_jsonl(archived, [later])
    import os
    import time

    os.utime(archived, (time.time() + 10, time.time() + 10))

    from tokuse import codex_usage

    report = codex_usage.collect(now)
    assert report["ok"] is True
    today = report["windows"]["today"]["totals"]
    assert today["input"] == 120
    assert today["output"] == 12
    assert today["reasoning"] == 8
    assert today["cache_read"] == 80
    names = [item["name"] for item in report["resets"]]
    assert names[0] == "5h"
    assert report["resets"][0]["used_percent"] == 25


def test_codex_live_window_label():
    from tokuse.codex_usage import resets_from_wham

    resets = resets_from_wham(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 2_592_000,
                    "reset_at": 1_800_000_000,
                },
                "secondary_window": {
                    "used_percent": 12,
                    "limit_window_seconds": 18_000,
                    "reset_at": 1_800_000_000,
                },
            }
        }
    )
    assert [item["name"] for item in resets] == ["Month", "5h"]
    assert resets[0]["used_percent"] == 0


def test_claude_dedupes_and_maps_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKUSE_CLAUDE_DIR", str(tmp_path))
    monkeypatch.delenv("TOKUSE_CLAUDE_USAGE_FIXTURE", raising=False)
    now = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)
    stamp = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    usage = {
        "input_tokens": 10,
        "output_tokens": 4,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 3,
    }
    records = [
        {
            "type": "assistant",
            "timestamp": stamp,
            "message": {"id": "m1", "model": "claude-sonnet", "usage": usage},
        },
        {
            "type": "assistant",
            "timestamp": stamp,
            "message": {"id": "m1", "model": "claude-sonnet", "usage": usage},
        },
        {
            "type": "assistant",
            "timestamp": stamp,
            "message": {"id": "m2", "model": "<synthetic>", "usage": usage},
        },
    ]
    _write_jsonl(tmp_path / "projects" / "demo" / "session.jsonl", records)
    from tokuse import claude_usage

    report = claude_usage.collect(now)
    assert report["windows"]["today"]["totals"]["input"] == 10
    assert report["windows"]["today"]["totals"]["cache_read"] == 7
    assert report["windows"]["today"]["totals"]["cache_write"] == 3
    assert report["windows"]["today"]["by_model"][0]["model"] == "claude-sonnet"
    assert report["resets"][0]["detail"] == "claude login required"


def test_cursor_fixture_menu(tmp_path, monkeypatch, capsys):
    fixture = tmp_path / "cursor"
    fixture.mkdir()
    (fixture / "period.json").write_text(
        json.dumps(
            {
                "billingCycleStart": 1_700_000_000_000,
                "billingCycleEnd": 1_800_000_000_000,
                "planUsage": {
                    "totalPercentUsed": 46.2,
                    "autoPercentUsed": 10,
                    "apiPercentUsed": 20,
                    "includedSpend": 500,
                    "limit": 2000,
                },
            }
        )
    )
    (fixture / "plan.json").write_text("{}")
    (fixture / "aggregated.json").write_text(
        json.dumps(
            {
                "aggregations": [
                    {
                        "modelIntent": "composer",
                        "inputTokens": "1000",
                        "outputTokens": "200",
                        "cacheReadTokens": "50",
                        "cacheWriteTokens": "25",
                        "totalCents": 12,
                    }
                ],
                "totalInputTokens": "1000",
                "totalOutputTokens": "200",
                "totalCacheReadTokens": "50",
                "totalCacheWriteTokens": "25",
            }
        )
    )
    monkeypatch.setenv("TOKUSE_CURSOR_FIXTURE", str(fixture))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex"))
    monkeypatch.setenv("TOKUSE_CLAUDE_DIR", str(tmp_path / "empty-claude"))
    monkeypatch.delenv("TOKUSE_CLAUDE_USAGE_FIXTURE", raising=False)
    assert main(["menu"]) == 0
    text = capsys.readouterr().out
    assert text.splitlines()[0].startswith("C 46%")
    report = build()
    row = report["providers"]["cursor"]["windows"]["billing"]["by_model"][0]
    assert row["model"] == "composer"
    assert row["input"] == 1000
    assert row["cents"] == 12
    assert "C 46%" in format_menu(report)
    label = report["providers"]["cursor"]["windows"]["billing"]["label"]
    assert label.startswith("Billing cycle (")
    assert "–" in label
    billing = next(item for item in report["providers"]["cursor"]["resets"] if item["name"] == "Billing cycle")
    assert "–" in billing["detail"]
