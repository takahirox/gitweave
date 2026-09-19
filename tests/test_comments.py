"""Offline comment action contracts; all GitHub traffic is mocked."""
import copy
import json
import os
import subprocess
import unittest
from unittest.mock import Mock, patch

from gitweave.actions import GitHubActions
from gitweave.adapters import agent_environment
from gitweave.graph import validate_graph
from gitweave.model import Failure


class CommentTests(unittest.TestCase):
    def setUp(self):
        self.git = Mock()
        self.actions = GitHubActions(self.git, "run")
        self.comments = []
        self.posts = []
        self.target = {"number": 9}
        self.lose_response = False
        self.actions.gh = Mock(side_effect=self.github)
        self.node = {"kind": "action", "action": "comment_issue", "workspace_base": 0,
                     "config": {"repository": "owner/repo", "number": 9, "body_path": "/0/message"}}
        self.context = {"instance_id": "comment-1", "inputs": [{"message": "Human findings", "data": {"text": "Structured findings"}}]}

    def github(self, *args):
        if "POST" in args:
            body = args[-1].removeprefix("body=")
            self.posts.append(body)
            comment = {"id": len(self.posts), "html_url": f"https://github.com/owner/repo/issues/9#issuecomment-{len(self.posts)}", "body": body}
            self.comments.append(comment)
            if self.lose_response:
                self.lose_response = False
                raise Failure("github", "response lost", retryable=True)
            return json.dumps(comment)
        if "/comments?" in args[-1]:
            page = int(args[-1].split("page=")[-1])
            return json.dumps(self.comments[(page - 1) * 100:page * 100])
        return json.dumps(self.target)

    def run_comment(self):
        return self.actions.run("comment", self.node, self.context)

    def test_both_actions_and_content_sources(self):
        for action in ("comment_issue", "comment_pr"):
            for source in ("message", "data/text", "literal"):
                with self.subTest(action=action, source=source):
                    self.setUp()
                    self.node["action"] = action
                    if action == "comment_pr":
                        self.target["pull_request"] = {"url": "pr"}
                    if source == "literal":
                        self.node["config"].pop("body_path")
                        self.node["config"]["body"] = "$(do not execute) {{literal}}"
                        expected = self.node["config"]["body"]
                    else:
                        self.node["config"]["body_path"] = "/0/" + source
                        expected = "Human findings" if source == "message" else "Structured findings"
                    result = self.run_comment()
                    self.assertTrue(self.posts[0].startswith(expected + "\n\n<!-- gitweave-comment:"))
                    self.assertEqual(result.data["id"], 1)
                    self.assertIn("#issuecomment-1", result.data["url"])
                    self.git.command.assert_not_called()
                    self.assertEqual(self.actions.published, {})
                    self.assertIsNone(self.actions.input_pr)

    def test_invalid_config_rejected_by_graph_and_action_before_io(self):
        invalid = [None, [], {}, {"unknown": True}, {"repository": "owner/.."},
                   {"repository": "https://github.com/owner/repo"}, {"repository": "owner/repo?x"},
                   {"repository": "../repo"}, {"repository": "owner/repo/extra"},
                   {"number": True}, {"number": 0}, {"number": -1}, {"number": "9"},
                   {"body": "also supplied"}, {"body_path": None}, {"body_path": "/0/data/~2"},
                   {"body_path": "{{inputs}}"}, {"body_path": "/00/message"}, {"publish_node": "pub"}]
        for update in invalid:
            with self.subTest(update=update):
                node = copy.deepcopy(self.node)
                if isinstance(update, dict) and update:
                    node["config"].update(update)
                else:
                    node["config"] = update
                for validate in (lambda: validate_graph({"version": 1, "nodes": {"c": node}, "flow": ["c"]}),
                                 lambda: self.actions.run("c", node, self.context)):
                    with self.assertRaises(Failure):
                        validate()
        self.actions.gh.assert_not_called()

    def test_blank_nonstring_and_missing_bodies_before_io(self):
        for body in ("", " \n\t", None, 3, False, {}, []):
            for literal in (False, True):
                with self.subTest(body=body, literal=literal):
                    self.setUp()
                    if literal:
                        self.node["config"].pop("body_path")
                        self.node["config"]["body"] = body
                    else:
                        self.context["inputs"][0]["message"] = body
                    with self.assertRaises(Failure):
                        self.run_comment()
                    self.actions.gh.assert_not_called()
        self.setUp()
        self.node["config"]["body_path"] = "/0/data/missing"
        with self.assertRaises(Failure):
            self.run_comment()
        self.actions.gh.assert_not_called()

    def test_target_type_number_and_missing_instance_rejected(self):
        for action, target in (("comment_pr", {"number": 9}),
                               ("comment_issue", {"number": 9, "pull_request": {}}),
                               ("comment_issue", {"number": 10}), ("comment_issue", {})):
            self.node["action"] = action
            self.target = target
            with self.assertRaises(Failure):
                self.run_comment()
        self.assertEqual(self.posts, [])
        self.actions.gh.reset_mock()
        self.context.pop("instance_id")
        with self.assertRaisesRegex(Failure, "instance_id"):
            self.run_comment()
        self.actions.gh.assert_not_called()

    def test_lost_success_reconciles_later_page_and_distinct_instances_post(self):
        for action in ("comment_issue", "comment_pr"):
            with self.subTest(action=action):
                self.setUp()
                self.node["action"] = action
                if action == "comment_pr":
                    self.target["pull_request"] = {}
                self.comments = [{"id": 1000 + i, "body": "Human comment", "html_url": "human"} for i in range(100)]
                humans = copy.deepcopy(self.comments)
                self.lose_response = True
                with self.assertRaises(Failure) as error:
                    self.run_comment()
                self.assertTrue(error.exception.retryable)
                # No in-memory publication state is required for reconciliation.
                self.actions = GitHubActions(self.git, "run")
                self.actions.gh = Mock(side_effect=self.github)
                self.assertEqual(self.run_comment().data["id"], 1)
                self.assertEqual(len(self.posts), 1)
                self.assertTrue(any("page=2" in c.args[-1] for c in self.actions.gh.call_args_list))
                self.context["instance_id"] = "comment-2"
                self.assertEqual(self.run_comment().data["id"], 2)
                self.assertEqual(self.run_comment().data["id"], 2)
                self.assertEqual(len(self.posts), 2)
                self.assertNotEqual(self.posts[0], self.posts[1])
                self.assertEqual(self.comments[:100], humans)
                self.assertFalse(any("PATCH" in c.args for c in self.actions.gh.call_args_list))

    def test_failures_before_post_and_malformed_success_retry(self):
        real = self.github
        for failed_call in (1, 2, 3):
            self.setUp()
            calls = 0
            def fail(*args):
                nonlocal calls
                calls += 1
                if calls == failed_call:
                    raise Failure("github", "unavailable", retryable=True)
                return real(*args)
            self.actions.gh.side_effect = fail
            with self.assertRaises(Failure):
                self.run_comment()
            self.actions.gh.side_effect = real
            self.assertEqual(self.run_comment().data["id"], 1)
            self.assertEqual(len(self.posts), 1)
        for response in ("", "{", "{}"):
            self.setUp()
            def malformed(*args):
                reply = real(*args)
                return response if "POST" in args else reply
            self.actions.gh.side_effect = malformed
            with self.assertRaises(Failure) as error:
                self.run_comment()
            self.assertTrue(error.exception.retryable)
            self.actions.gh.side_effect = real
            self.assertEqual(self.run_comment().data["id"], 1)
            self.assertEqual(len(self.posts), 1)

    def test_runtime_owned_transport_credentials(self):
        action = GitHubActions(self.git, "run")
        authority = {key: "fake-runtime" for key in (
            "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
            "GH_CONFIG_DIR", "SSH_AUTH_SOCK", "SSH_ASKPASS", "GIT_SSH_COMMAND",
            "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")}
        parent = dict(authority, GH_HOST="fake.enterprise.invalid", CUSTOM_TOOLCHAIN="fake-dev")
        with patch.dict(os.environ, parent, clear=True):
            self.assertEqual(agent_environment(), {"CUSTOM_TOOLCHAIN": "fake-dev"})
            with patch("gitweave.actions.subprocess.run", return_value=Mock(returncode=0, stdout="{}")) as command:
                action.gh("api", "repos/owner/repo/issues/9")
            # System Actions retain runtime authority and their existing fixed host.
            self.assertEqual(command.call_args.kwargs["env"], dict(parent, GH_HOST="github.com"))
            self.assertEqual(dict(os.environ), parent)
            self.assertEqual(agent_environment(), {"CUSTOM_TOOLCHAIN": "fake-dev"})
        for failure in (OSError("missing gh"), subprocess.TimeoutExpired("gh", 120)):
            with patch("gitweave.actions.subprocess.run", side_effect=failure):
                with self.assertRaises(Failure) as error:
                    action.gh("api", "target")
                self.assertTrue(error.exception.retryable)
