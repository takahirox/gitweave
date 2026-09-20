"""Deterministic concurrency checks using mocked Git/GitHub and thread handshakes."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import Mock

from gitweave.actions import GitHubActions
from test_pr_input import pull


class ObservedLock:
    """Expose a contending acquisition without depending on a scheduling delay."""
    def __init__(self):
        self.lock = threading.Lock()
        self.contending = threading.Event()

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.contending.set()
            self.lock.acquire()

    def __exit__(self, *args):
        self.lock.release()


class ActionConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.actions = GitHubActions(Mock(), "run")
        self.pub = {"action": "publish_pr", "config": {
            "repository": "owner/repo", "base": "main", "title": "Title"}}
        self.context = {"workspace_base": "a" * 40}

    def test_independent_publishers_input_sync_and_comments_overlap(self):
        barrier = threading.Barrier(5)

        def gh(*args):
            if args[:2] == ("pr", "list"):
                barrier.wait(timeout=5)
                return "[]"
            if args[:2] == ("pr", "create"):
                return "url"
            if args == ("api", "repos/owner/repo/pulls/10"):
                barrier.wait(timeout=5)
                return json.dumps(pull())
            if "POST" in args:
                return json.dumps({"id": 1, "html_url": "comment"})
            number = int(args[1].rsplit("/", 1)[1])
            barrier.wait(timeout=5)
            return json.dumps({"number": number, **({"pull_request": {}} if number == 2 else {})})

        self.actions.gh = Mock(return_value=json.dumps(pull()))
        self.actions.input_pr = self.actions.read_pr("owner/repo", 10)
        self.actions.remote_sha = "a" * 40
        self.actions.gh.side_effect = gh
        nodes = [("pub1", self.pub), ("pub2", self.pub), ("sync", {"action": "sync_pr"})]
        nodes += [(action, {"action": action, "config": {
            "repository": "owner/repo", "number": number, "body": "Comment"}})
            for number, action in enumerate(("comment_issue", "comment_pr"), 1)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(self.actions.run, name, node, self.context) for name, node in nodes]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(len(results), 5)
        self.assertEqual(self.actions.published, {"pub1": "a" * 40, "pub2": "a" * 40})

    def test_same_publisher_publication_and_merge_wait_for_publication(self):
        for next_action in ("publish_pr", "merge_pr"):
            with self.subTest(next_action=next_action):
                self.setUp()
                lock = ObservedLock()
                self.actions.publisher_locks["pub"] = lock
                entered, release = threading.Event(), threading.Event()
                created = []

                def gh(*args):
                    if args[:2] == ("pr", "list"):
                        if not created:
                            entered.set()
                            self.assertTrue(release.wait(timeout=5))
                        return json.dumps(created)
                    if args[:2] == ("pr", "create"):
                        created.append({"number": 1, "url": "url"})
                        return "url"
                    if args[:2] == ("pr", "edit"):
                        return ""
                    if args[:2] == ("pr", "view"):
                        return json.dumps(dict(number=1, state="OPEN", headRefOid="a" * 40,
                                               baseRefName="main", url="url"))
                    self.assertIn("sha=" + "a" * 40, args)
                    return json.dumps({"merged": True, "sha": "merged"})

                self.actions.gh = Mock(side_effect=gh)
                node = self.pub if next_action == "publish_pr" else {
                    "action": "merge_pr", "config": {"repository": "owner/repo", "publish_node": "pub"}}
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(self.actions.run, "pub", self.pub, self.context)
                    try:
                        self.assertTrue(entered.wait(timeout=5))
                        second = pool.submit(self.actions.run, "pub", node, {"workspace_base": "b" * 40})
                        self.assertTrue(lock.contending.wait(timeout=5))
                        self.assertEqual(self.actions.git.command.call_count, 1)
                        self.assertEqual(self.actions.gh.call_count, 1)
                    finally:
                        release.set()
                    first.result(timeout=5)
                    result = second.result(timeout=5)
                self.assertEqual(len(created), 1)
                if next_action == "publish_pr":
                    self.assertEqual(self.actions.published["pub"], "b" * 40)
                    self.assertEqual(self.actions.gh.call_args.args[:2], ("pr", "edit"))
                else:
                    self.assertTrue(result.data["merged"])

    def test_input_sync_and_merge_wait_for_known_head_update(self):
        for next_action in ("sync_pr", "merge_pr"):
            with self.subTest(next_action=next_action):
                self.setUp()
                raw = pull()
                self.actions.gh = Mock(side_effect=lambda *args: json.dumps(raw) if "PUT" not in args
                                       else json.dumps({"merged": True, "sha": "merged"}))
                self.actions.input_pr = self.actions.read_pr("owner/repo", 10)
                self.actions.remote_sha = "a" * 40
                lock = ObservedLock()
                self.actions.input_pr_lock = lock
                entered, release = threading.Event(), threading.Event()

                def command(*args):
                    if args[0] == "push" and not entered.is_set():
                        raw["head"]["sha"] = "b" * 40
                        entered.set()
                        self.assertTrue(release.wait(timeout=5))
                    return ""

                self.actions.git.command.side_effect = command
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(self.actions.run, "sync", {"action": "sync_pr"},
                                        {"workspace_base": "b" * 40})
                    try:
                        self.assertTrue(entered.wait(timeout=5))
                        second = pool.submit(self.actions.run, "next", {"action": next_action},
                                             {"workspace_base": "c" * 40})
                        self.assertTrue(lock.contending.wait(timeout=5))
                        self.assertEqual(self.actions.remote_sha, "a" * 40)
                        self.assertEqual(self.actions.gh.call_count, 2)
                    finally:
                        release.set()
                    first.result(timeout=5)
                    second.result(timeout=5)
                if next_action == "sync_pr":
                    self.assertIn("--force-with-lease=refs/heads/topic:" + "b" * 40,
                                  self.actions.git.command.call_args.args)
                    self.assertEqual(self.actions.remote_sha, "c" * 40)
                else:
                    self.assertIn("sha=" + "b" * 40, self.actions.gh.call_args.args)
