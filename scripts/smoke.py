"""Opt-in live smoke: local disposable repository, no publication or model switching."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gitweave.runtime import Runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["codex", "claude", "both"], default="both")
    args = parser.parse_args()
    repo = Path(tempfile.mkdtemp(prefix="gitweave-smoke-"))
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Smoke", "-c", "user.email=smoke@localhost", "commit", "--allow-empty", "-qm", "base"], check=True)
    providers = ["codex", "claude"] if args.provider == "both" else [args.provider]
    nodes = {p: {"kind": "agent", "provider": p, "workspace_base": 0,
                 "instruction": f"This is a tiny smoke test. Create {p}.txt containing only 'ok' and a newline. Do not delegate. Return message 'smoke passed' and data ok=true. Do not inspect unrelated files or run any network/publication commands.",
                 "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}}
             for p in providers}
    run = Runtime(json.dumps({"version": 1, "nodes": nodes, "flow": providers, "max_steps": 4, "retries": 0, "timeout": 180}), repo, "HEAD", "Validate native CLI adapters")
    record = run.run()
    print(json.dumps({"repository": str(repo), "run_id": run.id, "status": record["status"], "failure": record.get("failure"), "attempts": record["attempts"]}, indent=2))
    if record["status"] != "completed":
        return 1
    for output in record["outputs"]:
        for provider in providers:
            content = subprocess.check_output(["git", "-C", str(repo), "show", f"{output['commit']}:{provider}.txt"], text=True)
            if content.strip() != "ok":
                raise RuntimeError("Unexpected smoke artifact")
    print("Artifact and structured-result validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
