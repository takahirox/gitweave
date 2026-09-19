import argparse
import json
from pathlib import Path
import sys
from .model import Failure
from .runtime import Runtime


def main():
    parser = argparse.ArgumentParser(prog="gitweave")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Execute a JSON graph")
    run.add_argument("--graph", required=True, type=Path)
    run.add_argument("--repo", required=True, type=Path)
    run.add_argument("--commit", required=True)
    run.add_argument("request")
    args = parser.parse_args()
    try:
        record = Runtime(args.graph.read_text(), args.repo, args.commit, args.request).run()
        print(json.dumps({key: record[key] for key in ("run_id", "status", "run_ref", "notes_ref", "outputs")}, indent=2))
        if record["status"] != "completed":
            print(json.dumps(record["failure"]), file=sys.stderr)
            return 1
        return 0
    except (Failure, OSError, ValueError) as exc:
        print(f"gitweave: {exc}", file=sys.stderr)
        return 2
