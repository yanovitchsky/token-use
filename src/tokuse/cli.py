from __future__ import annotations

import json
import sys

from tokuse.report import build, format_human, format_menu


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = list(argv)
    as_json = False
    if "--json" in args:
        as_json = True
        args = [arg for arg in args if arg != "--json"]
    command = args[0] if args else "report"
    if command not in {"report", "menu"}:
        print("usage: tokuse [report|menu] [--json]", file=sys.stderr)
        return 2
    report = build()
    if command == "menu":
        sys.stdout.write(format_menu(report))
        return 0
    if as_json:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    sys.stdout.write(format_human(report))
    return 0
