import argparse
import json
from pathlib import Path
import sys
from .graph import validate_graph
from .model import Failure
from .runtime import Runtime
from .events import JSONEventSink


def main():
    parser = argparse.ArgumentParser(prog="gitweave")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Statically validate a JSON graph")
    validate.add_argument("--graph", required=True, type=Path)
    run = commands.add_parser("run", help="Execute a JSON graph")
    run.add_argument("--graph", required=True, type=Path)
    run.add_argument("--repo", required=True)
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--commit")
    source.add_argument("--pr", type=int)
    source.add_argument("--issue", type=int)
    run.add_argument("--base-branch", metavar="BRANCH",
                     help="With --issue, freeze this branch head as the Run base (default: remote default branch)")
    run.add_argument("--initialize-empty", metavar="BRANCH",
                     help="With --issue, initialize an empty remote on its configured default branch")
    run.add_argument("request", nargs="?", help="Optional additional operator guidance for nodes")
    run.add_argument("--provenance-remote")
    resume = commands.add_parser("resume", help="Continue a Run from completed node checkpoints")
    resume.add_argument("--run", required=True, dest="run_id")
    resume.add_argument("--repo", help="Local repository or bare store containing the Run")
    args = parser.parse_args()
    try:
        if args.command == "validate":
            validate_graph(json.loads(args.graph.read_text()))
            print(f"Graph passes static validation: {args.graph}")
            return 0
        if args.command == "resume":
            record = Runtime.resume(args.run_id, args.repo, event_sink=JSONEventSink(sys.stderr)).run()
        else:
            record = Runtime(args.graph.read_text(), args.repo, args.commit, args.request, pr=args.pr, issue=args.issue,
                             provenance_remote=args.provenance_remote, event_sink=JSONEventSink(sys.stderr),
                             initialize_empty=args.initialize_empty, base_branch=args.base_branch).run()
        print(json.dumps({key: record[key] for key in ("run_id", "status", "repository", "run_ref", "notes_ref", "outputs")}, indent=2))
        if record["status"] != "completed":
            print(json.dumps(record["failure"]), file=sys.stderr)
            return 1
        return 0
    except (Failure, OSError, ValueError) as exc:
        print(f"gitweave: {exc}", file=sys.stderr)
        return 2
