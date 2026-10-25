"""Inspect/export fixtures or serve read-only local Nessie data. No credentials needed."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from backend.demo.data import list_scenarios, load_scenario, validate_catalog


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Show available scenario ids")
    commands.add_parser("validate", help="Validate authored data and every generated snapshot")
    for command in ("facts", "export", "serve"):
        sub = commands.add_parser(command)
        sub.add_argument("--scenario", default="safe_payment")
        sub.add_argument("--as-of", type=date.fromisoformat, help="Rebase every date to YYYY-MM-DD")
        if command == "export":
            sub.add_argument("--output", type=Path, help="Write JSON here; default: stdout")
        if command == "serve":
            sub.add_argument("--port", type=int, default=5051)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            for case in list_scenarios():
                print(f"{case['id']:26} {case['profile']:10} {case['title']}")
            return
        if args.command == "validate":
            profiles, cases = validate_catalog()
            for case in cases["scenarios"]:
                load_scenario(case["id"])
            print(f"Valid: {len(profiles['profiles'])} fictional profiles, {len(cases['scenarios'])} scenarios.")
            return
        snapshot = load_scenario(args.scenario, as_of=args.as_of)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))

    if args.command == "serve":
        from backend.demo.nessie import FixtureRequestHandler, create_fixture_app
        print(f"SYNTHETIC / READ ONLY: {snapshot['scenario_id']} as of {snapshot['as_of']}")
        print(f"Customer: {snapshot['customer']['first_name']} Demo; no real Nessie calls or payments.")
        create_fixture_app(snapshot).run(host="127.0.0.1", port=args.port, debug=False,
                                         request_handler=FixtureRequestHandler)
        return
    if args.command == "facts":
        from backend.agent.facts import gather_facts
        from backend.demo.nessie import fixture_client
        client = fixture_client(snapshot)
        account = client.resolve(snapshot["customer"]["first_name"])
        payload = {"synthetic": True, "scenario": snapshot["scenario_id"],
                   "as_of": snapshot["as_of"],
                   "facts": gather_facts(client, account["_id"], ("balance", "bills", "deposits", "purchases"))}
    else:
        payload = snapshot
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.command == "export" and args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote synthetic snapshot to {args.output}")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
