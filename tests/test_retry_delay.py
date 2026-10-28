"""Retry scheduling with controlled async waits; no providers or timed sleeps."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from gitweave.model import Failure, Result
from gitweave.runtime import Runtime
from test_runtime import Fake, git, graph, node


class RetryDelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        git(self.repo, "init", "-q")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
            "commit", "--allow-empty", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def test_delay_precedence_and_retry_eligibility_for_both_node_kinds(self):
        # graph options, node options, failing attempts, retryable, attempts, waits
        cases = [
            ({"retries": 2}, {}, 2, True, 3, []),
            ({"retries": 2, "retry_delay_seconds": 0}, {}, 2, True, 3, []),
            ({"retries": 2, "retry_delay_seconds": 0.25}, {}, 2, True, 3, [0.25, 0.25]),
            ({"retries": 1, "retry_delay_seconds": 60}, {"retry_delay_seconds": 0.5}, 1, True, 2, [0.5]),
            ({"retries": 1}, {"retry_delay_seconds": 0.5}, 1, True, 2, [0.5]),
            ({"retries": 1, "retry_delay_seconds": 60}, {"retry_delay_seconds": 0}, 1, True, 2, []),
            ({"retry_delay_seconds": 60}, {}, 1, True, 1, []),
            ({"retries": 0, "retry_delay_seconds": 60}, {}, 1, True, 1, []),
            ({"retries": 2, "retry_delay_seconds": 60}, {"retries": 0}, 1, True, 1, []),
            ({"retries": 0, "retry_delay_seconds": 60}, {"retries": 1}, 1, True, 2, [60]),
            ({"retries": 2, "retry_delay_seconds": 60}, {}, 1, False, 1, []),
            ({"retries": 2, "retry_delay_seconds": 60}, {}, 3, True, 3, [60, 60]),
            ({"retries": 2, "retry_delay_seconds": 60}, {}, 0, True, 1, []),
        ]
        for kind in ("agent", "command"):
            for options, override, failures, retryable, attempts, waits in cases:
                with self.subTest(kind=kind, options=options, override=override,
                                  failures=failures, retryable=retryable):
                    timeline = []
                    calls = []

                    def work(n, context, workspace, timeout):
                        self.assertEqual(timeout, 0.125)
                        calls.append(context)
                        timeline.append("attempt")
                        if len(calls) <= failures:
                            raise Failure("provider" if retryable else "usage_limit", "native failure",
                                          retryable=retryable)
                        return Result(message="ok")

                    async def sleep(delay):
                        # The failed attempt has already been durably retained.
                        self.assertEqual(run.git.load_attempts()[-1]["status"], "failed")
                        timeline.append(delay)

                    spec = node(**override) if kind == "agent" else dict(kind="command", argv=["unused"], **override)
                    adapter = Mock(run=Mock(side_effect=work))
                    run = Runtime(graph({"a": spec}, ["a"], max_steps=1, timeout=0.125, **options),
                                  self.repo, self.base, adapters={"fake": adapter})
                    with patch("gitweave.runtime.run_command", side_effect=work), \
                            patch("gitweave.runtime.asyncio.sleep", side_effect=sleep) as sleeper:
                        record = run.run()
                    self.assertEqual(len(calls), attempts)
                    self.assertEqual(record["steps"], 1)
                    self.assertEqual(record["status"], "completed" if failures < attempts else "failed")
                    self.assertEqual([c.args[0] for c in sleeper.await_args_list], waits)
                    expected = ["attempt"]
                    for index in range(attempts - 1):
                        expected += ([waits[index]] if waits else []) + ["attempt"]
                    self.assertEqual(timeline, expected)
                    self.assertEqual([a["attempt"] for a in record["attempts"]], list(range(1, attempts + 1)))
                    self.assertEqual(len({a["invocation_id"] for a in record["attempts"]}), 1)

    def test_wait_yields_preserves_admission_and_checks_stop_before_retry(self):
        for concurrency, fail_sibling in ((1, False), (2, False), (2, True)):
            with self.subTest(concurrency=concurrency, fail_sibling=fail_sibling):
                seen = []

                def work(n, c, w):
                    name = n["instruction"]
                    seen.append(name)
                    if name == "a" and seen.count("a") == 1:
                        raise Failure("provider", "transient", retryable=True)
                    if name == "b" and fail_sibling:
                        raise Failure("usage_limit", "native rejection")
                    return Result()

                run = Runtime(graph({name: node(name) for name in ("a", "b", "after")},
                                    [{"parallel": [["a"], ["b"]]}, "after"],
                                    concurrency=concurrency, retries=1, retry_delay_seconds=60),
                              self.repo, self.base, adapters={"fake": Fake(work)})

                async def exercise():
                    waiting, release, sibling_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
                    original_node = run.node

                    async def ordered_node(name, *args):
                        if name == "b":
                            await waiting.wait()
                        try:
                            return await original_node(name, *args)
                        finally:
                            if name == "b":
                                sibling_done.set()

                    async def sleep(delay):
                        self.assertEqual(delay, 60)
                        waiting.set()
                        await release.wait()

                    async def independent_work():
                        await waiting.wait()
                        try:
                            if concurrency == 1:
                                self.assertTrue(run.semaphore.locked())
                                self.assertNotIn("b", seen)
                            else:
                                await sibling_done.wait()
                                self.assertIn("b", seen)
                                self.assertEqual(run.stopped, fail_sibling)
                            self.assertEqual(seen.count("a"), 1)
                        finally:
                            release.set()

                    with patch.object(run, "node", side_effect=ordered_node), \
                            patch("gitweave.runtime.asyncio.sleep", side_effect=sleep):
                        record, _ = await asyncio.gather(run.execute(), independent_work())
                    return record

                # Only a deadlock guard; all ordering uses events, not elapsed time.
                record = asyncio.run(asyncio.wait_for(exercise(), timeout=10))
                self.assertEqual(record["status"], "failed" if fail_sibling else "completed")
                self.assertEqual(seen.count("a"), 1 if fail_sibling else 2)
                self.assertEqual("after" in seen, not fail_sibling)
                self.assertEqual(next(a["status"] for a in record["attempts"] if a["instance_id"].startswith("b-")),
                                 "failed" if fail_sibling else "completed")

    def test_resume_retains_delay_checkpoints_inputs_and_invocation_accounting(self):
        def initial(n, c, w):
            if n["instruction"] == "a":
                (w / "artifact").write_text("completed")
                return Result(message="upstream", data={"value": 1})
            raise Failure("provider", "transient", retryable=True)

        definition = graph({"a": node("a"), "b": node("b", retry_delay_seconds=0.25)},
                           ["a", "b"], retries=1, retry_delay_seconds=60, max_steps=2)
        run = Runtime(definition, self.repo, self.base, adapters={"fake": Fake(initial)})
        with patch("gitweave.runtime.asyncio.sleep") as sleeper:
            failed = run.run()
        sleeper.assert_awaited_once_with(0.25)
        self.assertEqual(failed["status"], "failed")
        previous = run.git.load_attempts()
        upstream = previous[0]["output_commit"]
        contexts, workspaces = [], []

        def resumed(n, c, w):
            self.assertEqual(n["instruction"], "b")
            self.assertEqual(git(w, "rev-parse", "HEAD"), upstream)
            self.assertEqual((w / "artifact").read_text(), "completed")
            self.assertFalse((w / "partial").exists())
            self.assertEqual(c["inputs"][0]["data"], {"value": 1})
            workspaces.append(w)
            contexts.append(json.loads(json.dumps(c)))
            if len(contexts) == 1:
                (w / "partial").touch()
                c["inputs"][0]["data"]["value"] = 99
                raise Failure("provider", "transient", retryable=True,
                              result=Result(raw_stdout="retained", session_id="session"))
            return Result(message="recovered")

        restored = Runtime.resume(run.id, self.repo, adapters={"fake": Fake(resumed)})
        with patch("gitweave.runtime.asyncio.sleep") as sleeper:
            record = restored.run()
        sleeper.assert_awaited_once_with(0.25)
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual((record["run_id"], record["graph"], record["steps"]), (run.id, definition, 2))
        self.assertEqual(contexts[0], contexts[1])
        self.assertNotEqual(workspaces[0], workspaces[1])
        history = restored.git.load_attempts()
        self.assertEqual(history[:len(previous)], previous)
        self.assertEqual([a["attempt"] for a in history], [1, 1, 2, 3, 4])
        self.assertEqual(len({a["invocation_id"] for a in history if a["node_id"] == "b"}), 1)
        self.assertEqual(history[-2]["result"]["raw_stdout"], "retained")
        self.assertEqual(history[-2]["result"]["session_id"], "session")
        for attempt in history[1:]:
            self.assertEqual(attempt["workspace_base"], upstream)
            self.assertEqual(attempt["inputs"], previous[1]["inputs"])
        self.assertEqual(restored.git.load_run()["graph"], definition)
