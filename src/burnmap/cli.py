"""Command line entry point for burnmap."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .analyze import LOOP_REPEAT_THRESHOLD, analyze_session
from .parser import parse_lines, parse_session
from .pricing import PriceBook
from .report import render_json, render_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="burnmap",
        description="Map where an AI coding agent session actually burns tokens and money.",
        epilog=(
            "exit codes: 0 ok, 1 io/parse failure, 2 waste budget exceeded. "
            "Feeding a session is usually: burnmap run ~/.claude/projects/*/*.jsonl"
        ),
    )
    parser.add_argument("files", nargs="*", help="session JSONL file(s); '-' reads stdin")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--top", type=int, default=8, help="tools to show in the ranking (default 8)")
    parser.add_argument(
        "--loop-threshold",
        type=int,
        default=LOOP_REPEAT_THRESHOLD,
        help=f"consecutive identical calls that count as a loop (default {LOOP_REPEAT_THRESHOLD})",
    )
    parser.add_argument(
        "--fail-over",
        type=float,
        metavar="USD",
        help="exit 2 when estimated avoidable spend exceeds this amount (CI gate)",
    )
    parser.add_argument(
        "--pricing",
        metavar="FILE",
        help="JSON file of rate overrides, e.g. {\"my-model\": {\"input\": 0.5, \"output\": 1.5}}",
    )
    return parser


def _load(args) -> list:
    turns: list = []
    if not args.files:
        return parse_lines(sys.stdin)
    for path in args.files:
        if path == "-":
            turns.extend(parse_lines(sys.stdin))
            continue
        try:
            turns.extend(parse_session(path))
        except OSError as exc:
            print(f"burnmap: cannot read {path}: {exc}", file=sys.stderr)
            raise SystemExit(1)
    return turns


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.loop_threshold < 2:
        parser.error("--loop-threshold must be 2 or greater")

    book = PriceBook()
    if args.pricing:
        try:
            book = PriceBook.from_file(args.pricing)
        except (OSError, ValueError) as exc:
            print(f"burnmap: cannot read pricing file: {exc}", file=sys.stderr)
            return 1

    turns = _load(args)
    if not turns:
        print("burnmap: no usable usage records found in the input.", file=sys.stderr)
        return 1

    smap = analyze_session(turns, book=book, loop_threshold=args.loop_threshold)
    print(render_json(smap) if args.json else render_text(smap, top=args.top))

    if args.fail_over is not None and smap.waste_cost_usd > args.fail_over:
        print(
            f"\nburnmap: avoidable spend {smap.waste_cost_usd:.4f} USD exceeds "
            f"--fail-over {args.fail_over:.4f} USD",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
