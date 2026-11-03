"""Argument parsing only. Command modules do the work."""

from __future__ import annotations

import argparse
import json
import sys

from orchestrator.board import CommandError, acknowledge, add, answer, count_waiting, done, scan, snooze
from orchestrator.config import Config, load_config
from orchestrator.deliver import deliver, open_target, route


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orch")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("scan")
    sub.add_parser("count")
    add_cmd = sub.add_parser("add")
    add_cmd.add_argument("project")
    add_cmd.add_argument("kind")
    add_cmd.add_argument("where")
    add_cmd.add_argument("text")
    add_cmd.add_argument("--unblocks")
    add_cmd.add_argument("--gate")
    answer_cmd = sub.add_parser("answer")
    answer_cmd.add_argument("ref")
    answer_cmd.add_argument("text")
    snooze_cmd = sub.add_parser("snooze")
    snooze_cmd.add_argument("ref")
    snooze_cmd.add_argument("when")
    done_cmd = sub.add_parser("done")
    done_cmd.add_argument("ref")
    sweep_cmd = sub.add_parser("sweep")
    sweep_cmd.add_argument("--json", action="store_true")
    sweep_cmd.add_argument("--ack")
    route_cmd = sub.add_parser("route")
    route_cmd.add_argument("ref")
    route_cmd.add_argument("--json", action="store_true")
    deliver_cmd = sub.add_parser("deliver")
    deliver_cmd.add_argument("project")
    deliver_cmd.add_argument("text")
    deliver_cmd.add_argument("--reset", choices=("none", "clear", "compact"), default="none")
    deliver_cmd.add_argument("--dry-run", action="store_true")
    open_cmd = sub.add_parser("open")
    open_cmd.add_argument("ref")
    return parser


def _print_board(config: Config) -> int:
    sys.stdout.write(scan(config).text)
    return 0


def _sweep(config: Config, as_json: bool, ack: str | None) -> int:
    if ack:
        matched = acknowledge(config, ack)
        for session in matched:
            print(f"acked {session}")
        return 0
    result = scan(config)
    rows = [item for item in result.items if item["source"] == "sweep"]
    if as_json:
        payload = [
            {
                "project": item["project"],
                "session": item["session"],
                "transcript": item["transcript"],
                "ended": item["since"],
                "reason": item["reason"],
            }
            for item in rows
        ]
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    for item in rows:
        print(f"{item['project']}  {item['session']}  ended {item['since']}  {item['transcript']}")
    return 0


def main(argv: list[str] | None = None, config: Config | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "count":
        try:
            if config is None:
                config = load_config()
            waiting = count_waiting(config)
            if waiting:
                print(f"⚑ {waiting}")
            return 0
        except Exception:
            return 0
    try:
        if config is None:
            config = load_config()
        if args.cmd in (None, "scan"):
            return _print_board(config)
        if args.cmd == "add":
            sys.stdout.write(add(
                config, args.project, args.kind, args.where, args.text,
                unblocks=args.unblocks, gate=args.gate,
            ).text)
            return 0
        if args.cmd == "answer":
            sys.stdout.write(answer(config, args.ref, args.text).text)
            return 0
        if args.cmd == "snooze":
            sys.stdout.write(snooze(config, args.ref, args.when).text)
            return 0
        if args.cmd == "done":
            sys.stdout.write(done(config, args.ref).text)
            return 0
        if args.cmd == "sweep":
            return _sweep(config, args.json, args.ack)
        if args.cmd == "route":
            report = route(config, args.ref)
            if args.json:
                json.dump(report.as_dict(), sys.stdout, indent=2)
                sys.stdout.write("\n")
            else:
                sys.stdout.write(report.text())
            return 0
        if args.cmd == "deliver":
            sys.stdout.write(deliver(
                config, args.project, args.text, reset=args.reset, dry_run=args.dry_run,
            ))
            return 0
        if args.cmd == "open":
            sys.stdout.write(open_target(config, args.ref))
            return 0
    except CommandError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 2
