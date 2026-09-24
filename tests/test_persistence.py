"""Deterministic Git push/fetch against local bare remotes; no external services."""
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from gitweave.cli import main
from gitweave.git import Git
from gitweave.model import Failure, Result
from gitweave.persistence import destination, persist
from gitweave.runtime import Runtime
from test_runtime import Fake, git, graph, node


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--allow-empty", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.remote = self.root / "remote.git"
        git(self.repo, "init", "--bare", "-q", str(self.remote))

    def run_local(self, *, connected=False, work=None):
        if connected:
            git(self.repo, "remote", "add", "origin", str(self.remote))
        run = Runtime(graph({"work": node()}, ["work"], retries=1), self.repo, self.base,
                      "persistence test", adapters={"fake": Fake(work or (lambda *a: Result(message="done")))})
        run.run()
        return run

    def refs(self, repo, run):
        return git(repo, "for-each-ref", "--format=%(refname) %(objectname)",
                   f"refs/gitweave/{run.id}/", run.git.notes)

    def refspecs(self, run):
        prefix = f"refs/gitweave/{run.id}/"
        return [prefix + "*:" + prefix + "*", run.git.notes + ":" + run.git.notes]

    def test_final_action_and_retries_survive_merge_and_source_deletion(self):
        calls = []
        def work(n, c, w):
            calls.append(1)
            if len(calls) == 1:
                raise Failure("provider", "retry", retryable=True, result=Result(raw_stderr="failed log"))
            (w / "artifact").write_text("final artifact")
            return Result(message="success", raw_stdout="success log", usage={"tokens": 12})
        remote, source = self.remote, self.repo
        def final(n, c, w):
            artifact = c["inputs"][0]["commit"]
            # Simulate external publication, merge and branch deletion; no files change.
            git(remote, "fetch", "--no-tags", "-q", str(source), artifact + ":refs/heads/topic")
            git(remote, "update-ref", "refs/heads/main", artifact)
            git(remote, "update-ref", "-d", "refs/heads/topic")
            return Result(data={"merged": True, "merge_commit": artifact})
        run = Runtime(graph({"work": node(), "final": node("final")}, ["work", "final"], retries=1),
                      self.repo, self.base, "request",
                      adapters={"fake": Fake(lambda n, c, w: final(n, c, w) if n["instruction"] == "final" else work(n, c, w))},
                      provenance_remote=str(self.remote))
        git(self.repo, "update-ref", "refs/gitweave/other/run", self.base)
        git(self.repo, "update-ref", "refs/notes/gitweave/other", self.base)
        git(self.repo, "update-ref", run.git.notes + "-other", self.base)
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "tag", "-a", "unrelated", "-m", "not provenance")
        git(self.repo, "config", "push.followTags", "true")
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        expected = self.refs(self.repo, run)
        self.assertEqual(self.refs(self.remote, run), expected)
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads"), "refs/heads/main")
        self.assertEqual(git(self.remote, "for-each-ref", "refs/tags", "refs/gitweave/other", "refs/notes/gitweave/other"), "")
        self.assertEqual(git(self.remote, "for-each-ref", run.git.notes + "-other"), "")
        self.assertNotIn("/archive ", expected)
        shutil.rmtree(self.repo)
        fresh = self.root / "fresh"
        git(self.root, "init", "-q", str(fresh))
        git(fresh, "fetch", "--no-tags", str(self.remote), *self.refspecs(run))
        self.assertEqual(self.refs(fresh, run), expected)
        self.assertEqual(json.loads(git(fresh, "show", record["run_ref"] + ":run.json")), record)
        notes = [json.loads(git(fresh, "notes", "--ref=" + run.git.notes, "show", a["commit"])) for a in record["attempts"]]
        self.assertEqual([n["status"] for n in notes], ["failed", "completed", "completed"])
        self.assertEqual(notes[0]["result"]["raw_stderr"], "failed log")
        self.assertEqual(notes[1]["result"]["usage"], {"tokens": 12})
        self.assertTrue(notes[2]["result"]["data"]["merged"])
        self.assertEqual(git(fresh, "show", record["outputs"][0]["commit"] + ":artifact"), "final artifact")

    def test_origin_push_without_atomic_support_leaves_artifact_branches_unchanged(self):
        git(self.repo, "push", "-q", str(self.remote), "HEAD:refs/heads/main")
        git(self.remote, "config", "receive.advertiseAtomic", "false")
        git(self.repo, "remote", "add", "origin", str(self.root / "fetch-only"))
        git(self.repo, "config", "remote.origin.pushurl", str(self.remote))
        run = self.run_local()
        self.assertEqual(json.loads(git(self.remote, "show", run.record["run_ref"] + ":run.json")), run.record)
        self.assertEqual(self.refs(self.remote, run), self.refs(self.repo, run))
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_offline_run_can_be_pushed_and_fetched_with_git(self):
        run = self.run_local()
        self.assertIsNone(run.record["provenance_destination"])
        # Existing historical refs are ordinary Git objects, including old markers.
        marker = f"refs/gitweave/{run.id}/archive"
        git(self.repo, "update-ref", marker, self.base)
        for name in ("head", "base"):
            git(self.repo, "update-ref", f"refs/gitweave/{run.id}/input/{name}", self.base)
        git(self.repo, "push", str(self.remote), *self.refspecs(run))
        fresh = self.root / "fresh"
        git(self.root, "clone", "-q", str(self.remote), str(fresh))
        git(fresh, "fetch", "origin", *self.refspecs(run))
        self.assertEqual(self.refs(fresh, run), self.refs(self.repo, run))
        self.assertEqual(git(fresh, "rev-parse", marker), self.base)

    def test_partial_push_failure_is_visible_and_native_retry_completes(self):
        git(self.repo, "remote", "add", "origin", str(self.remote))
        hook = self.remote / "hooks" / "update"
        hook.write_text('#!/bin/sh\ncase "$1" in refs/notes/*) exit 1;; esac\n')
        hook.chmod(0o755)
        with self.assertRaisesRegex(Failure, "hook declined"):
            self.run_local()
        run_ref = git(self.repo, "for-each-ref", "--format=%(refname)", "refs/gitweave").splitlines()[-1]
        record = json.loads(git(self.repo, "show", run_ref + ":run.json"))
        self.assertEqual(record["status"], "completed")
        self.assertIn("ended_at", record)
        self.assertEqual(json.loads(git(self.remote, "show", run_ref + ":run.json")), record)
        self.assertEqual(git(self.remote, "for-each-ref", record["notes_ref"]), "")
        hook.unlink()
        prefix = f"refs/gitweave/{record['run_id']}/"
        for _ in range(2):
            git(self.repo, "push", "origin", prefix + "*:" + prefix + "*", record["notes_ref"] + ":" + record["notes_ref"])
        self.assertEqual(git(self.remote, "rev-parse", record["notes_ref"]), git(self.repo, "rev-parse", record["notes_ref"]))

    def test_cli_reports_persistence_failure(self):
        git(self.repo, "remote", "add", "origin", str(self.root / "missing"))
        run = Runtime(graph({"work": node()}, ["work"]), self.repo, self.base,
                      "request", adapters={"fake": Fake(lambda *a: Result())})
        path = self.root / "graph.json"
        path.write_text(run.record["graph"])
        with patch("sys.argv", ["gitweave", "run", "--repo", str(self.repo), "--commit", self.base,
                                "--graph", str(path), "request"]), patch("gitweave.cli.Runtime", return_value=run), \
                patch("sys.stdout", new_callable=io.StringIO) as out, patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(), 2)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("Provenance push failed", err.getvalue())
            self.assertIn(str(self.root / "missing"), err.getvalue())
            self.assertIn("does not appear to be a git repository", err.getvalue())
        self.assertEqual(json.loads(git(self.repo, "show", run.record["run_ref"] + ":run.json")), run.record)
        attempt = run.record["attempts"][0]
        note = json.loads(git(self.repo, "notes", "--ref=" + run.git.notes, "show", attempt["commit"]))
        self.assertEqual(note["status"], "completed")

    def test_failed_run_before_any_attempt_has_no_notes_to_push(self):
        git(self.repo, "remote", "add", "origin", str(self.remote))
        run = Runtime(graph({"work": node()}, ["work"]), self.repo, self.base,
                      "request", adapters={"fake": Fake(lambda *a: Result())})
        with patch.object(run, "flow", side_effect=Failure("test", "before attempt")):
            record = run.run()
        self.assertEqual(record["status"], "failed")
        self.assertEqual(json.loads(git(self.remote, "show", record["run_ref"] + ":run.json")), record)

    def test_failed_attempt_is_persisted(self):
        def work(*args):
            raise Failure("provider", "stopped")
        run = self.run_local(connected=True, work=work)
        self.assertEqual(run.record["status"], "failed")
        self.assertEqual(self.refs(self.remote, run), self.refs(self.repo, run))

    def test_push_uses_normal_git_authentication_for_all_destinations(self):
        refs = ["refs/gitweave/run/run", "refs/notes/gitweave/run"]
        for target in ("https://github.com/owner/repo.git", "origin",
                       "git@github.com:owner/repo.git", str(self.remote)):
            with self.subTest(target=target):
                storage = Mock(run_id="run", notes=refs[1])
                storage.command.side_effect = ["\n".join(refs), ""]
                persist(storage, target)
                self.assertEqual([c.args for c in storage.command.call_args_list], [
                    ("for-each-ref", "--format=%(refname)", "refs/gitweave/run/", refs[1]),
                    ("push", "--no-follow-tags", "--", target,
                     *[f"{ref}:{ref}" for ref in refs])])

    def test_destination_selection(self):
        storage = Git(self.repo, "selected")
        self.assertIsNone(destination(storage, {}))
        git(self.repo, "remote", "add", "origin", str(self.remote))
        self.assertEqual(destination(storage, {}), "origin")
        self.assertEqual(destination(storage, {"github_repository": "owner/repo"}), "https://github.com/owner/repo.git")
        self.assertEqual(destination(storage, {"github_repository": "owner/repo"}, "git@example.test:repo.git"),
                         "git@example.test:repo.git")
        self.assertEqual(destination(storage, {"github_repository": None}, "chosen"), "chosen")

    def test_transfer_error_preserves_git_diagnostic_and_local_refs(self):
        run = self.run_local()
        retained = self.refs(self.repo, run)
        original = run.git.command
        def command(*args, **kwargs):
            if "push" in args:
                raise failure
            return original(*args, **kwargs)
        for diagnostic in ("fatal: Authentication failed for 'https://example.test/repo.git'",
                           "ssh: connect to host example.test port 22: Connection refused\n"
                           "fatal: Could not read from remote repository."):
            with self.subTest(diagnostic=diagnostic):
                failure = Failure("git", diagnostic, retryable=True)
                with patch.object(run.git, "command", side_effect=command):
                    with self.assertRaises(Failure) as caught:
                        persist(run.git, "origin")
                self.assertEqual(caught.exception.kind, "persistence")
                self.assertFalse(caught.exception.retryable)
                self.assertTrue(str(caught.exception).endswith("\n" + diagnostic))
                self.assertIs(caught.exception.__cause__, failure)
                self.assertEqual(self.refs(self.repo, run), retained)
