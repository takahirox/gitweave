import json
import signal
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch
from gitweave.adapters import CLIAdapter
from gitweave.runtime import Runtime
from gitweave.model import Failure, Result


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def node(instruction="work", **options):
    return dict(kind="agent", provider="fake", instruction=instruction, workspace_base=0, **options)


def graph(nodes, flow, **options):
    return json.dumps(dict(version=1, nodes=nodes, flow=flow, **options))


class Fake:
    def __init__(self, fn):
        self.fn = fn

    def run(self, node, context, workspace, timeout):
        return self.fn(node, context, workspace)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--allow-empty", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def runtime(self, nodes, flow, fn, **options):
        return Runtime(graph(nodes, flow, **options), self.repo, self.base, "request", adapters={"fake": Fake(fn)})

    def note(self, run, commit):
        return json.loads(git(self.repo, "notes", f"--ref={run.git.notes}", "show", commit))

    def test_node_requirements_are_checked_only_on_selected_branch(self):
        cases = [
            (dict(node(), provider="missing"), "graph", "Provider is not registered: missing"),
        ]
        for action in ("sync_pr", "merge_pr"):
            for config in ({}, {"config": {}}):
                cases.append((dict(kind="action", action=action, workspace_base=0, **config),
                              "pr_input", "This action requires an existing-PR Run input"))
        for optional, kind, message in cases:
            for selected in (False, True):
                with self.subTest(optional=optional, selected=selected):
                    nodes = {"route": node(schema={"type": "boolean"}),
                             "optional": optional, "after": node()}
                    flow = ["route", {"if": {"condition": {"path": "/0/data", "equals": True},
                                             "then": ["optional"], "else": []}}, "after"]
                    work = Mock(return_value=Result(data=selected))
                    run = self.runtime(nodes, flow, work, retries=2)
                    with patch.object(run.actions, "gh") as gh:
                        record = run.run()
                    gh.assert_not_called()
                    self.assertEqual(record["status"], "failed" if selected else "completed")
                    self.assertEqual(work.call_count, 1 if selected else 2)
                    notes = [self.note(run, attempt["commit"]) for attempt in record["attempts"]]
                    self.assertEqual([note["node_id"] for note in notes],
                                     ["route", "optional" if selected else "after"])
                    if selected:
                        self.assertEqual(record["failure"], {"kind": kind, "message": message})
                        self.assertEqual(notes[-1]["failure"],
                                         {"kind": kind, "message": message, "retryable": False})
                        self.assertEqual(notes[-1]["status"], "failed")
                    stored = json.loads(git(self.repo, "show", record["run_ref"] + ":run.json"))
                    self.assertEqual(stored, record)

    def test_static_errors_in_unselected_branch_still_fail_initialization(self):
        nodes = {"route": node(schema={"type": "boolean"}),
                 "invalid": dict(kind="action", action="sync_pr", workspace_base=0,
                                 config={"repository": "owner/repo"})}
        flow = ["route", {"if": {"condition": {"path": "/0/data", "equals": True},
                                 "then": ["invalid"], "else": []}}]
        work = Mock(return_value=Result(data=False))
        with self.assertRaisesRegex(Failure, "input PR actions require empty config"):
            self.runtime(nodes, flow, work)
        work.assert_not_called()

    def test_sequential_artifacts_results_and_run_record(self):
        def work(n, c, w):
            if n["instruction"] == "first":
                (w / "artifact.txt").write_text("hello")
                return Result(message="plan", data={"ok": True}, usage={"input_tokens": 10})
            self.assertEqual((w / "artifact.txt").read_text(), "hello")
            self.assertEqual(c["inputs"][0]["message"], "plan")
            return Result(message="finished")
        run = self.runtime({"a": node("first"), "b": node()}, ["a", "b"], work)
        record = run.run()
        self.assertEqual(record["status"], "completed")
        a, b = [x["commit"] for x in record["attempts"]]
        self.assertEqual(git(self.repo, "rev-parse", f"{b}^"), a)
        self.assertEqual(git(self.repo, "rev-parse", f"{a}^{{tree}}"), git(self.repo, "rev-parse", f"{b}^{{tree}}"))
        self.assertEqual(self.note(run, a)["result"]["usage"]["input_tokens"], 10)
        stored = json.loads(git(self.repo, "show", f"{record['run_ref']}:run.json"))
        self.assertEqual(stored["graph"], run.record["graph"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertFalse((self.repo / "artifact.txt").exists())
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)

    def test_run_record_is_written_only_at_finalization(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                calls = []

                def work(n, c, w):
                    self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)",
                                         f"refs/gitweave/{run.id}/run"), "")
                    # Earlier attempts are already durable before the Run is finalized.
                    for attempt in run.record["attempts"]:
                        ref = (f"refs/gitweave/{run.id}/attempts/"
                               f"{attempt['instance_id']}/{attempt['attempt']}")
                        self.assertEqual(git(self.repo, "rev-parse", ref), attempt["commit"])
                        self.assertEqual(self.note(run, attempt["commit"])["status"], attempt["status"])
                    calls.append(c)
                    if len(calls) == 1 or fail:
                        raise Failure("provider", "transient", retryable=True)
                    return Result(message="done")

                run = self.runtime({"a": node(), "b": node()}, ["a", "b"], work, retries=1)
                with patch.object(run.git, "run_record", wraps=run.git.run_record) as write:
                    record = run.run()
                write.assert_called_once_with(record)
                self.assertEqual(record["status"], "failed" if fail else "completed")
                self.assertEqual(len(record["attempts"]), 2 if fail else 3)
                stored = json.loads(git(self.repo, "show", record["run_ref"] + ":run.json"))
                self.assertEqual(stored, record)
                if fail:
                    self.assertEqual(stored["failure"]["kind"], "provider")
                else:
                    self.assertEqual(stored["outputs"][0]["message"], "done")

    def test_timeout_controls_session_and_reaches_subprocess_wait_unchanged(self):
        for provider in ("codex", "claude"):
            raw = (Path(__file__).parent / "fixtures" / f"{provider}.jsonl").read_text()
            for options in ({}, {"timeout": 1800}, {"timeout": 0.25}):
                with self.subTest(provider=provider, options=options):
                    agent = node()
                    agent["provider"] = provider
                    run = Runtime(graph({"a": agent}, ["a"], **options), self.repo,
                                  self.base, "request", adapters={provider: CLIAdapter(provider)})
                    child = Mock(returncode=0)
                    child.communicate.return_value = (raw, "")
                    launch = Mock(return_value=child)
                    with patch("gitweave.adapters.subprocess",
                               Mock(Popen=launch, PIPE=subprocess.PIPE,
                                    TimeoutExpired=subprocess.TimeoutExpired)), \
                            patch("gitweave.adapters.os.killpg") as kill:
                        record = run.run()
                    self.assertEqual(record["status"], "completed", record.get("failure"))
                    launch.assert_called_once()
                    self.assertIs(launch.call_args.kwargs["start_new_session"], "timeout" in options)
                    child.communicate.assert_called_once()
                    self.assertEqual(child.communicate.call_args.kwargs, {"timeout": options.get("timeout")})
                    kill.assert_not_called()
                    self.assertEqual("timeout" in run.graph, "timeout" in options)

    def test_explicit_timeout_retains_evidence_and_obeys_retry_policy(self):
        for retries, recover in ((0, False), (2, False), (1, True)):
            with self.subTest(retries=retries, recover=recover):
                agent = node()
                agent["provider"] = "codex"
                run = Runtime(graph({"a": agent}, ["a"], timeout=0.25, retries=retries),
                              self.repo, self.base, "request", adapters={"codex": CLIAdapter("codex")})
                children = []
                for index in range(retries + 1):
                    child = Mock(pid=12345 + index, returncode=-signal.SIGKILL)
                    child.communicate.side_effect = [
                        subprocess.TimeoutExpired("codex", 0.25),
                        (f"partial stdout {index}", f"partial stderr {index}"),
                    ]
                    children.append(child)
                if recover:
                    children[-1].communicate.side_effect = [('{"type":"turn.completed","usage":{}}', "")]
                    children[-1].returncode = 0
                launch = Mock(side_effect=children)
                with patch("gitweave.adapters.subprocess",
                           Mock(Popen=launch, PIPE=subprocess.PIPE,
                                TimeoutExpired=subprocess.TimeoutExpired)), \
                        patch("gitweave.adapters.os.killpg") as kill:
                    record = run.run()
                self.assertEqual(record["status"], "completed" if recover else "failed")
                if not recover:
                    self.assertEqual(record["failure"]["kind"], "timeout")
                self.assertEqual(len(record["attempts"]), retries + 1)
                failures = children[:-1] if recover else children
                self.assertEqual(kill.call_args_list, [call(c.pid, signal.SIGKILL) for c in failures])
                self.assertTrue(all(c.kwargs["start_new_session"] for c in launch.call_args_list))
                notes = [self.note(run, a["commit"]) for a in record["attempts"]]
                self.assertEqual(len({n["instance_id"] for n in notes}), 1)
                for index, (child, attempt, note) in enumerate(zip(children, record["attempts"], notes)):
                    self.assertEqual(child.communicate.call_args_list[0].kwargs, {"timeout": 0.25})
                    self.assertEqual(note["attempt"], index + 1)
                    self.assertEqual(note["workspace_base"], self.base)
                    self.assertEqual(note["input_commits"], [self.base])
                    self.assertEqual(note["node_id"], "a")
                    self.assertEqual(note["provider"], "codex")
                    self.assertIn("started_at", note)
                    self.assertIn("ended_at", note)
                    self.assertGreaterEqual(note["duration_seconds"], 0)
                    self.assertEqual(git(self.repo, "rev-parse", attempt["commit"] + "^"), self.base)
                    ref = f"refs/gitweave/{run.id}/attempts/{note['instance_id']}/{index + 1}"
                    self.assertEqual(git(self.repo, "rev-parse", ref), attempt["commit"])
                    if index < len(failures):
                        self.assertEqual(child.communicate.call_args_list[1], call())
                        self.assertEqual(note["status"], "failed")
                        self.assertEqual(note["failure"]["kind"], "timeout")
                        self.assertTrue(note["failure"]["retryable"])
                        self.assertEqual(note["result"]["raw_stdout"], f"partial stdout {index}")
                        self.assertEqual(note["result"]["raw_stderr"], f"partial stderr {index}")
                stored = json.loads(git(self.repo, "show", f"{record['run_ref']}:run.json"))
                self.assertEqual(stored["attempts"], record["attempts"])
                self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)

    def test_parallel_isolation_join_and_input_order(self):
        barrier = threading.Barrier(2)
        def work(n, c, w):
            name = n["instruction"]
            if name in ("left", "right"):
                self.assertFalse((w / "file").exists())
                (w / "file").write_text(name)
                barrier.wait(timeout=5)
                return Result(message=name)
            self.assertEqual([i["message"] for i in c["inputs"]], ["left", "right"])
            self.assertEqual((w / "file").read_text(), "right")
            self.assertEqual(git(w, "show", c["inputs"][0]["commit"] + ":file"), "left")
            (w / "file").write_text("integrated")
            return Result(message="joined")
        join = node("join")
        join["workspace_base"] = 1
        run = self.runtime({"a": node("left"), "b": node("right"), "j": join},
                           [{"parallel": [["a"], ["b"]]}, "j"], work, concurrency=2)
        self.assertEqual(run.run()["status"], "completed")

    def test_map_loop_and_conditional(self):
        def work(n, c, w):
            if n["instruction"] == "plan":
                return Result(data={"tasks": ["a", "b", "c"]})
            if n["instruction"] == "worker":
                return Result(message=c["item"], data={"repeat": False})
            self.assertEqual(len(c["inputs"]), 3)
            return Result(data={"approved": True})
        schema = {"type": "object", "properties": {"tasks": {"type": "array", "items": {"type": "string"}}}, "required": ["tasks"]}
        flow = ["plan", {"map": {"path": "/0/data/tasks", "flow": [{"loop": {"flow": ["worker"], "while": {"path": "/0/data/repeat", "equals": True}}}]}},
                "join", {"if": {"condition": {"path": "/0/data/approved", "equals": True}, "then": ["plan"], "else": ["bad"]}}]
        run = self.runtime({"plan": node("plan", schema=schema), "worker": node("worker", schema={"type": "object"}), "join": node("join", schema={"type": "object"}), "bad": node("bad")}, flow, work)
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        attempts = [self.note(run, a["commit"]) for a in record["attempts"]]
        workers = [a for a in attempts if a["node_id"] == "worker"]
        self.assertEqual(len({a["instance_id"] for a in workers}), 3)
        self.assertEqual({a["fan_out_origin"]["index"] for a in workers}, {0, 1, 2})
        self.assertEqual(len({a["workspace_base"] for a in workers}), 1)
        self.assertFalse(any(a["node_id"] == "bad" for a in attempts))

    def test_retry_is_from_original_input_and_failure_is_retained(self):
        calls = []
        def work(n, c, w):
            self.assertFalse((w / "bad").exists())
            calls.append(w)
            if len(calls) == 1:
                (w / "bad").write_text("partial")
                raise Failure("provider", "transient", retryable=True, result=Result(raw_stdout="log", session_id="session"))
            return Result(message="ok")
        run = self.runtime({"a": node()}, ["a"], work, retries=1)
        record = run.run()
        self.assertEqual(record["status"], "completed")
        failed, success = [a["commit"] for a in record["attempts"]]
        for sha in (failed, success):
            self.assertEqual(git(self.repo, "rev-parse", f"{sha}^"), self.base)
        self.assertNotEqual(failed, success)
        self.assertEqual(self.note(run, failed)["result"]["session_id"], "session")
        self.assertIn(failed, git(self.repo, "show-ref"))
        self.assertEqual(git(self.repo, "rev-list", success).splitlines(), [success, self.base])

    def test_limit_and_invalid_result_do_not_silently_retry(self):
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        run = self.runtime({"a": node(schema=schema)}, ["a"], lambda *args: Result(data={"ok": "yes"}), retries=3)
        record = run.run()
        self.assertEqual(record["failure"]["kind"], "result")
        self.assertEqual(len(record["attempts"]), 1)
        run = self.runtime({"a": node(schema={"type": "boolean"})}, [{"loop": {"flow": ["a"], "while": {"path": "/0/data", "equals": True}}}], lambda *args: Result(data=True), max_steps=5)
        record = run.run()
        self.assertEqual(record["failure"]["kind"], "step_limit")
        self.assertLessEqual(record["steps"], 5)

    def test_retryability_flag_controls_retries_regardless_of_kind(self):
        for retryable in (False, True):
            with self.subTest(retryable=retryable):
                def limited(*args):
                    raise Failure("usage_limit", "limit", retryable=retryable)
                run = self.runtime({"a": node()}, ["a"], limited, retries=2)
                record = run.run()
                self.assertEqual(len(record["attempts"]), 3 if retryable else 1)
                self.assertEqual(record["failure"]["kind"], "usage_limit")

    def test_provider_failure_text_follows_configured_retries(self):
        for provider in ("codex", "claude"):
            success = (Path(__file__).parent / "fixtures" / f"{provider}.jsonl").read_text()
            for retries, recover in ((0, False), (2, False), (1, True)):
                with self.subTest(provider=provider, retries=retries, recover=recover):
                    agent = dict(node(), provider=provider)
                    run = Runtime(graph({"a": agent}, ["a"], retries=retries), self.repo,
                                  self.base, "request", adapters={provider: CLIAdapter(provider)})
                    outputs = [(1, "usage limit reached", "insufficient credits")] * (retries + 1)
                    if recover:
                        outputs[-1] = (0, success, "")
                    with patch("gitweave.adapters.process", side_effect=outputs) as invoke:
                        record = run.run()
                    self.assertEqual(record["status"], "completed" if recover else "failed")
                    self.assertEqual(invoke.call_count, retries + 1)
                    if not recover:
                        self.assertEqual(record["failure"]["message"], "insufficient credits")
                    for attempt in record["attempts"][:1 if recover else retries + 1]:
                        note = self.note(run, attempt["commit"])
                        self.assertEqual(note["failure"]["message"], "insufficient credits")
                        self.assertEqual(note["failure"]["kind"], "provider")
                        self.assertTrue(note["failure"]["retryable"])
                        self.assertEqual(note["result"]["raw_stdout"], "usage limit reached")
                        self.assertEqual(note["result"]["raw_stderr"], "insufficient credits")

    def test_native_provider_diagnostic_reaches_run_and_provenance(self):
        message = "Authentication failed: please sign in again"
        for provider, event in [
            ("codex", {"type": "turn.failed", "error": {"message": message}}),
            ("claude", {"type": "result", "subtype": "error_during_execution",
                        "is_error": True, "errors": [message]}),
        ]:
            with self.subTest(provider=provider):
                run = Runtime(graph({"a": dict(node(), provider=provider)}, ["a"]),
                              self.repo, self.base, "request", adapters={provider: CLIAdapter(provider)})
                raw = json.dumps(event)
                with patch("gitweave.adapters.process", return_value=(1, raw, "stderr context")):
                    record = run.run()
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["failure"]["message"], message)
                for attempt in record["attempts"]:
                    note = self.note(run, attempt["commit"])
                    self.assertEqual(note["failure"]["message"], message)
                    self.assertEqual(note["result"]["native"]["events"], [event])
                    self.assertEqual(note["result"]["raw_stdout"], raw)
                    self.assertEqual(note["result"]["raw_stderr"], "stderr context")

    def test_empty_map_preserves_upstream(self):
        def work(n, c, w):
            if n["instruction"] == "plan":
                return Result(data=[])
            self.assertEqual(c["inputs"][0]["data"], [])
            return Result(message="joined")
        run = self.runtime({"plan": node("plan", schema={"type": "array"}), "worker": node(), "join": node()},
                           ["plan", {"map": {"path": "/0/data", "flow": ["worker"]}}, "join"], work)
        self.assertEqual(len(run.run()["attempts"]), 2)

    def test_parallel_github_comments_overlap(self):
        barrier = threading.Barrier(2)
        nodes = {action: dict(kind="action", action=action, workspace_base=0,
                             config={"repository": "owner/repo", "number": number, "body": "Findings"})
                 for number, action in enumerate(("comment_issue", "comment_pr"), 1)}
        run = self.runtime(nodes, [{"parallel": [[name] for name in nodes]}], None, concurrency=2)

        def gh(*args):
            if "POST" in args:
                # Both actions must reach the POST before either can complete.
                barrier.wait(timeout=5)
                number = int(args[3].split("/")[-2])
                return json.dumps({"id": number, "html_url": f"comment/{number}"})
            number = int(args[1].rsplit("/", 1)[1])
            return json.dumps({"number": number, **({"pull_request": {}} if number == 2 else {})})

        run.actions.gh = Mock(side_effect=gh)
        with patch("gitweave.runtime.persist"):
            record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(sorted(output["data"]["id"] for output in record["outputs"]), [1, 2])
        self.assertEqual(run.actions.gh.call_count, 4)

    def test_system_action_retry_and_result_handoff(self):
        class Actions:
            count = 0
            def run(self, name, node, context):
                self.count += 1
                if self.count == 1:
                    raise Failure("github", "transient", retryable=True)
                return Result(data={"url": "https://example.test/pr"})
        publish = dict(kind="action", action="publish_pr", workspace_base=0,
                       config={"repository": "owner/repo", "base": "main", "title": "PR"})
        def work(n, c, w):
            self.assertEqual(c["inputs"][0]["data"]["url"], "https://example.test/pr")
            return Result(message="done")
        run = self.runtime({"pub": publish, "a": node()}, ["pub", "a"], work, retries=1)
        run.actions = Actions()
        with patch("gitweave.runtime.persist") as push:
            record = run.run()
            push.assert_called_once_with(run.git, "https://github.com/owner/repo.git")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(len(record["attempts"]), 3)

    def test_running_sibling_is_retained_before_failure_finishes(self):
        barrier = threading.Barrier(2)
        def work(n, c, w):
            barrier.wait(timeout=5)
            if n["instruction"] == "bad":
                raise Failure("provider", "failed")
            return Result(message="done")
        run = self.runtime({"a": node("bad"), "b": node()}, [{"parallel": [["a"], ["b"]]}], work)
        with patch.object(run.git, "run_record", wraps=run.git.run_record) as write:
            record = run.run()
        write.assert_called_once_with(record)
        self.assertEqual(record["status"], "failed")
        self.assertEqual({a["status"] for a in record["attempts"]}, {"failed", "completed"})
        stored = json.loads(git(self.repo, "show", record["run_ref"] + ":run.json"))
        self.assertEqual(stored, record)
        for attempt in stored["attempts"]:
            self.assertEqual(self.note(run, attempt["commit"])["status"], attempt["status"])

    def test_comment_loop_retry_posts_and_retains_diagnostics(self):
        from unittest.mock import Mock
        comment = dict(kind="action", action="comment_pr", workspace_base=0,
                       config={"repository": "owner/repo", "number": 9, "body_path": "/0/message"})
        reviews = []
        def review(n, c, w):
            reviews.append(c)
            return Result(message=f"Review {len(reviews)}")
        def decide(n, c, w):
            self.assertEqual(c["inputs"][0]["data"]["id"], len(reviews) * 2)
            return Result(data=len(reviews) < 2)
        run = self.runtime({"review": node("review"), "comment": comment,
                            "decide": node("decide", schema={"type": "boolean"})},
                           [{"loop": {"flow": ["review", "comment", "decide"],
                                      "while": {"path": "/0/data", "equals": True}}}],
                           lambda n, c, w: review(n, c, w) if n["instruction"] == "review" else decide(n, c, w),
                           retries=1)
        comments = []
        def github(*args):
            if "POST" in args:
                comments.append({"id": len(comments) + 1, "html_url": "https://example.test/comment",
                                 "body": args[-1].removeprefix("body=")})
                if len(comments) % 2:
                    raise Failure("github", "Response lost", retryable=True)
                return json.dumps(comments[-1])
            return json.dumps({"number": 9, "pull_request": {}})
        run.actions.gh = Mock(side_effect=github)
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        attempts = [self.note(run, a["commit"]) for a in record["attempts"]]
        comments_attempts = [a for a in attempts if a["node_id"] == "comment"]
        self.assertEqual(len(comments_attempts), 4)
        self.assertEqual([a["status"] for a in comments_attempts], ["failed", "completed"] * 2)
        self.assertEqual(len({a["instance_id"] for a in comments_attempts}), 2)
        self.assertEqual(comments_attempts[0]["instance_id"], comments_attempts[1]["instance_id"])
        self.assertEqual(comments_attempts[0]["failure"]["message"], "Response lost")
        self.assertEqual(comments_attempts[1]["result"]["data"]["id"], 2)
        self.assertEqual(comments_attempts[3]["result"]["data"]["id"], 4)
        self.assertEqual([c["body"] for c in comments], ["Review 1", "Review 1", "Review 2", "Review 2"])
        self.assertEqual(reviews[0]["instance_id"], attempts[0]["instance_id"])

    def test_storage_failure_keeps_workspace(self):
        from unittest.mock import patch
        import shutil
        run = self.runtime({"a": node()}, ["a"], lambda n, c, w: (w / "artifact").write_text("saved") and Result())
        with patch.object(run.git, "retain", side_effect=Failure("storage", "disk unavailable")):
            record = run.run()
        self.assertEqual(record["status"], "failed")
        entries = git(self.repo, "worktree", "list", "--porcelain").splitlines()
        paths = [Path(e.removeprefix("worktree ")) for e in entries if e.startswith("worktree ")][1:]
        self.assertEqual(len(paths), 1)
        self.assertEqual((paths[0] / "artifact").read_text(), "saved")
        run.git.remove_worktree(paths[0])
        shutil.rmtree(paths[0].parent)

    def test_cleanup_failure_keeps_registered_worktree(self):
        from unittest.mock import patch
        import shutil
        run = self.runtime({"a": node()}, ["a"], lambda *args: Result())
        with patch.object(run.git, "remove_worktree", side_effect=Failure("cleanup", "busy")):
            record = run.run()
        self.assertEqual(record["status"], "completed")
        note = self.note(run, record["attempts"][0]["commit"])
        self.assertEqual(note["cleanup_warning"], "busy")
        paths = [Path(e.removeprefix("worktree ")) for e in git(self.repo, "worktree", "list", "--porcelain").splitlines() if e.startswith("worktree ")][1:]
        self.assertTrue(paths[0].exists())
        run.git.remove_worktree(paths[0])
        shutil.rmtree(paths[0].parent)

    def test_control_rejects_unvalidated_data(self):
        run = self.runtime({"a": node()}, ["a", {"if": {"condition": {"path": "/0/data", "equals": True}, "then": [], "else": []}}], lambda *args: Result(data=True))
        self.assertEqual(run.run()["failure"]["kind"], "result")

    def test_loop_converges_and_empty_conditional_passes_through(self):
        calls = []
        def work(n, c, w):
            calls.append(c)
            return Result(data=len(calls) < 3)
        run = self.runtime({"a": node(schema={"type": "boolean"})},
                           [{"loop": {"flow": ["a"], "while": {"path": "/0/data", "equals": True}}},
                            {"if": {"condition": {"path": "/0/data", "equals": False}, "then": [], "else": ["a"]}}], work)
        record = run.run()
        self.assertEqual(record["status"], "completed")
        self.assertEqual(len(calls), 3)
        self.assertFalse(record["outputs"][0]["data"])
        self.assertEqual(calls[1]["workspace_base"], record["attempts"][0]["commit"])

    def test_nested_parallel_obeys_global_concurrency_bound(self):
        barrier = threading.Barrier(2)
        count, peak = 0, 0
        lock = threading.Lock()
        def work(n, c, w):
            nonlocal count, peak
            with lock:
                count += 1
                peak = max(peak, count)
            barrier.wait(timeout=5)
            with lock:
                count -= 1
            return Result()
        run = self.runtime({"a": node()}, [{"parallel": [[{"parallel": [["a"], ["a"], ["a"]]}], ["a"]]}], work, concurrency=2)
        self.assertEqual(run.run()["status"], "completed")
        self.assertEqual(peak, 2)
