"""Offline comment action contracts; all GitHub traffic is mocked."""
import copy
import json
import os
import subprocess
import unittest
from unittest.mock import Mock, call, patch

from gitweave.actions import GitHubActions
from gitweave.graph import validate_graph
from gitweave.model import Failure


class CommentTests(unittest.TestCase):
    def setUp(self):
        self.git = Mock()
        self.actions = GitHubActions(self.git, "run")
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
            if self.lose_response:
                self.lose_response = False
                raise Failure("github", "response lost", retryable=True)
            return json.dumps(comment)
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
                        self.node["config"]["body"] = "  $(do not execute) {{literal}}\n\nText\n"
                        expected = self.node["config"]["body"]
                    else:
                        self.node["config"]["body_path"] = "/0/" + source
                        expected = "Human findings" if source == "message" else "Structured findings"
                    result = self.run_comment()
                    self.assertEqual(self.posts, [expected])
                    self.assertEqual(self.actions.gh.call_args_list, [
                        call("api", "repos/owner/repo/issues/9"),
                        call("api", "--method", "POST", "repos/owner/repo/issues/9/comments",
                             "-f", f"body={expected}"),
                    ])
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

    def test_target_type_and_number_rejected(self):
        for action, target in (("comment_pr", {"number": 9}),
                               ("comment_issue", {"number": 9, "pull_request": {}}),
                               ("comment_issue", {"number": 10}), ("comment_issue", {})):
            self.node["action"] = action
            self.target = target
            with self.assertRaises(Failure):
                self.run_comment()
        self.assertEqual(self.posts, [])

    def test_comments_do_not_require_instance_identity(self):
        self.context.pop("instance_id")
        self.assertEqual(self.run_comment().data, {
            "id": 1, "url": "https://github.com/owner/repo/issues/9#issuecomment-1",
            "repository": "owner/repo", "number": 9})

    def test_each_invocation_posts_including_after_lost_response(self):
        for action in ("comment_issue", "comment_pr"):
            with self.subTest(action=action):
                self.setUp()
                self.node["action"] = action
                if action == "comment_pr":
                    self.target["pull_request"] = {}
                self.lose_response = True
                with self.assertRaises(Failure) as error:
                    self.run_comment()
                self.assertTrue(error.exception.retryable)
                self.assertEqual(self.run_comment().data["id"], 2)
                self.assertEqual(self.run_comment().data["id"], 3)
                self.assertEqual(self.posts, ["Human findings"] * 3)
                self.assertEqual(self.actions.gh.call_count, 6)

    def test_request_failures_reported_without_internal_retry(self):
        for failed_call in (1, 2):
            with self.subTest(failed_call=failed_call):
                self.setUp()
                failure = Failure("github", "unavailable", retryable=True)
                self.actions.gh.side_effect = ([json.dumps(self.target)] * (failed_call - 1)
                                               + [failure])
                with self.assertRaises(Failure) as error:
                    self.run_comment()
                self.assertIs(error.exception, failure)
                self.assertEqual(self.actions.gh.call_count, failed_call)

    def test_malformed_success_reported_and_next_invocation_posts(self):
        for response in ("", "{", "{}", "[]", '{"id": true, "html_url": "url"}',
                         '{"id": 0, "html_url": "url"}', '{"id": 1, "html_url": ""}'):
            with self.subTest(response=response):
                self.setUp()
                def malformed(*args):
                    reply = self.github(*args)
                    return response if "POST" in args else reply
                self.actions.gh.side_effect = malformed
                with self.assertRaises(Failure) as error:
                    self.run_comment()
                self.assertTrue(error.exception.retryable)
                self.assertEqual(self.actions.gh.call_count, 2)
                self.actions.gh.side_effect = self.github
                self.assertEqual(self.run_comment().data["id"], 2)
                self.assertEqual(self.posts, ["Human findings"] * 2)

    def test_runtime_owned_transport_credentials(self):
        action = GitHubActions(self.git, "run")
        authority = {key: "fake-runtime" for key in (
            "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
            "GH_CONFIG_DIR", "SSH_AUTH_SOCK", "SSH_ASKPASS", "GIT_SSH_COMMAND",
            "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")}
        parent = dict(authority, GH_HOST="fake.enterprise.invalid", CUSTOM_TOOLCHAIN="fake-dev")
        with patch.dict(os.environ, parent, clear=True):
            with patch("gitweave.actions.subprocess.run", return_value=Mock(returncode=0, stdout="{}")) as command:
                action.gh("api", "repos/owner/repo/issues/9")
            # System Actions retain runtime authority and their existing fixed host.
            self.assertEqual(command.call_args.kwargs["env"], dict(parent, GH_HOST="github.com"))
            self.assertEqual(dict(os.environ), parent)
        for failure in (OSError("missing gh"), subprocess.TimeoutExpired("gh", 120)):
            with patch("gitweave.actions.subprocess.run", side_effect=failure):
                with self.assertRaises(Failure) as error:
                    action.gh("api", "target")
                self.assertTrue(error.exception.retryable)
