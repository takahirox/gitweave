"""Deterministic archives against local bare remotes; no external services."""
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from gitweave.cli import main
from gitweave.git import Git
from gitweave.model import Failure, Result
from gitweave.persistence import Archive, destination
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
                      "archive test", adapters={"fake": Fake(work or (lambda *a: Result(message="done")))})
        run.run()
        return run

    def clone(self):
        clone = self.root / "fresh"
        git(self.root, "clone", "-q", str(self.remote), str(clone))
        return clone

    def test_final_retry_input_action_recovered_after_source_deletion(self):
        calls = []
        def work(*args):
            calls.append(1)
            if len(calls) == 1:
                raise Failure("provider", "retry", retryable=True, result=Result(raw_stderr="failed log"))
            return Result(message="success", raw_stdout="success log", usage={"tokens": 12})
        run = self.run_local(work=work)
        # Model an older runtime's completed PR Run, including its last System Action.
        run.git.command("update-ref", f"refs/gitweave/{run.id}/input/head", self.base)
        run.git.command("update-ref", f"refs/gitweave/{run.id}/input/base", self.base)
        final = run.git.empty(self.base, "merge action")
        run.git.retain(final, "attempts/merge-2/1", {"result": Result(data={"merged": True, "merge_commit": self.base}).record(), "status": "completed"})
        run.record["attempts"].append(dict(instance_id="merge-2", attempt=1, commit=final, status="completed"))
        run.record["outputs"] = [{"commit": final, "data": {"merged": True}}]
        run.git.run_record(run.record)
        git(self.repo, "update-ref", "refs/gitweave/other/run", self.base)
        git(self.repo, "update-ref", "refs/notes/gitweave/other", self.base)
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "tag", "-a", "unrelated", "-m", "not provenance")
        git(self.repo, "config", "push.followTags", "true")
        archive = Archive(run.git)
        archive.export(str(self.remote))
        expected = archive.local()
        self.assertEqual(archive.remote(str(self.remote)), expected)
        remote_refs = git(self.remote, "for-each-ref", "--format=%(refname)").splitlines()
        self.assertEqual(set(remote_refs), set(expected))
        # Artifact integration and deletion are independent of the archive.
        git(self.repo, "push", "-q", str(self.remote), f"{final}:refs/heads/main", f"{final}:refs/heads/topic")
        git(self.repo, "push", "-q", str(self.remote), ":refs/heads/topic")
        self.assertEqual(archive.remote(str(self.remote)), expected)
        record = run.record
        shutil.rmtree(self.repo)
        fresh = self.clone()
        restored = Archive(Git(fresh, run.id))
        restored.recover("origin")
        self.assertEqual(restored.local(), expected)
        self.assertEqual(restored.record(), record)
        notes = [json.loads(git(fresh, "notes", "--ref=" + run.git.notes, "show", a["commit"])) for a in record["attempts"]]
        self.assertEqual([n["status"] for n in notes], ["failed", "completed", "completed"])
        self.assertEqual(notes[0]["result"]["raw_stderr"], "failed log")
        self.assertEqual(notes[1]["result"]["usage"], {"tokens": 12})
        self.assertTrue(notes[2]["result"]["data"]["merged"])
        restored.recover("origin")
        restored.export("origin")
        self.assertEqual(restored.local(), expected)

    def test_runtime_finalizes_to_origin_without_touching_branches(self):
        run = self.run_local(connected=True)
        archive = Archive(run.git)
        remote_record = json.loads(git(self.remote, "show", run.record["run_ref"] + ":run.json"))
        self.assertEqual(remote_record, run.record)
        self.assertEqual(remote_record["status"], "completed")
        self.assertIn("ended_at", remote_record)
        self.assertEqual(archive.remote(str(self.remote)), archive.local())
        self.assertEqual(git(self.remote, "for-each-ref", "refs/heads"), "")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_offline_and_cli_export_fetch(self):
        run = self.run_local()
        self.assertIsNone(run.record["provenance_destination"])
        with self.assertRaisesRegex(Failure, "No provenance destination"):
            Archive(run.git).export()
        fresh = self.clone()
        for command, repo in (("export", self.repo), ("fetch", fresh)):
            with patch("sys.argv", ["gitweave", command, "--repo", str(repo), "--run", run.id,
                                     "--remote", str(self.remote)]), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(main(), 0)
                self.assertEqual(json.loads(out.getvalue())["run_id"], run.id)

    def test_partial_remote_and_local_conflicts(self):
        run = self.run_local()
        archive = Archive(run.git)
        git(self.repo, "push", "-q", str(self.remote), f"{self.base}:{archive.prefix}run")
        with self.assertRaisesRegex(Failure, "partially"):
            archive.export(str(self.remote))
        with self.assertRaisesRegex(Failure, "complete archive"):
            archive.recover(str(self.remote))
        git(self.remote, "update-ref", "-d", archive.prefix + "run")
        archive.export(str(self.remote))
        fresh = self.clone()
        restored = Archive(Git(fresh, run.id))
        git(fresh, "fetch", "-q", str(self.remote), self.base)
        git(fresh, "update-ref", archive.prefix + "run", self.base)
        with self.assertRaisesRegex(Failure, "Local Run namespace conflicts"):
            restored.recover("origin")
        self.assertEqual(restored.local(), {archive.prefix + "run": self.base})
        git(self.remote, "update-ref", "-d", archive.prefix + "attempts/work-1/1")
        with self.assertRaisesRegex(Failure, "invalid provenance"):
            restored.recover("origin")
        with self.assertRaisesRegex(Failure, "partially"):
            archive.export(str(self.remote))

    def test_missing_remote_and_atomic_rejection_preserve_evidence(self):
        run = self.run_local()
        archive = Archive(run.git)
        for remote in (str(self.root / "missing"), str(self.remote)):
            if remote == str(self.remote):
                git(self.remote, "config", "receive.advertiseAtomic", "false")
            with self.assertRaisesRegex(Failure, "transfer failed"):
                archive.export(remote)
            self.assertEqual(archive.record()["status"], "completed")
            self.assertIn(archive.prefix + "attempts/work-1/1", archive.local())
        self.assertEqual(archive.remote(str(self.remote)), {})

    def test_runtime_persistence_failure_is_not_success(self):
        git(self.repo, "remote", "add", "origin", str(self.root / "missing"))
        with self.assertRaisesRegex(Failure, "transfer failed"):
            self.run_local()
        refs = git(self.repo, "for-each-ref", "--format=%(refname)", "refs/gitweave").splitlines()
        run_ref = next(ref for ref in refs if ref.endswith("/run"))
        record = json.loads(git(self.repo, "show", run_ref + ":run.json"))
        self.assertEqual(record["status"], "completed")
        self.assertIn("ended_at", record)

    def test_destinations_and_credentials(self):
        storage = Git(self.repo, "selected")
        record = {"graph": graph({"p": {"action": "publish_pr", "config": {"repository": "owner/artifact"}}}, [])}
        git(self.repo, "remote", "add", "origin", str(self.remote))
        self.assertEqual(destination(storage, record), "https://github.com/owner/artifact.git")
        with self.assertRaisesRegex(Failure, "artifact repository"):
            destination(storage, record, "origin")
        for url in ("https://user:secret@example.test/repo", "https://example.test/repo?token=secret"):
            with self.assertRaises(Failure) as caught:
                destination(storage, {}, url)
            self.assertNotIn("secret", str(caught.exception))
        archive = Archive(storage)
        with patch.object(storage, "command", side_effect=Failure("git", "password secret")):
            with self.assertRaises(Failure) as caught:
                archive.remote(str(self.remote))
            self.assertNotIn("secret", str(caught.exception))
        with self.assertRaisesRegex(Failure, "Invalid Run ID"):
            Archive(Git(self.repo, "../other"))

    def test_changed_local_archive_is_rejected(self):
        run = self.run_local()
        archive = Archive(run.git)
        archive.export(str(self.remote))
        git(self.repo, "update-ref", archive.prefix + "run", self.base)
        with self.assertRaisesRegex(Failure, "missing or invalid"):
            archive.export(str(self.remote))

    def test_publication_race_is_atomic_and_never_overwrites(self):
        run = self.run_local()
        archive = Archive(run.git)
        original = run.git.command
        def command(*args, **kwargs):
            if "push" in args:
                # Competing writer claims the same Run after ls-remote.
                git(self.repo, "push", "-q", str(self.remote), f"{self.base}:{archive.prefix}run")
            return original(*args, **kwargs)
        with patch.object(run.git, "command", side_effect=command):
            with self.assertRaisesRegex(Failure, "transfer failed"):
                archive.export(str(self.remote))
        self.assertEqual(archive.remote(str(self.remote)), {archive.prefix + "run": self.base})

    def test_failed_final_run_is_archived(self):
        def work(*args):
            raise Failure("provider", "stopped")
        run = self.run_local(connected=True, work=work)
        remote_record = json.loads(git(self.remote, "show", run.record["run_ref"] + ":run.json"))
        self.assertEqual(remote_record, run.record)
        self.assertEqual(remote_record["status"], "failed")

    def test_multiple_destinations_require_artifact_selection(self):
        storage = Git(self.repo, "selected")
        record = {"graph": graph({name: {"action": "publish_pr", "config": {"repository": name + "/artifact"}}
                                  for name in ("first", "second")}, [])}
        with self.assertRaisesRegex(Failure, "Multiple artifact"):
            destination(storage, record)
        self.assertEqual(destination(storage, record, "https://github.com/second/artifact.git"),
                         "https://github.com/second/artifact.git")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "config", "--add", "remote.origin.pushurl", str(self.remote))
        git(self.repo, "config", "--add", "remote.origin.pushurl", str(self.root / "other"))
        with self.assertRaisesRegex(Failure, "exactly one"):
            destination(storage, {})

    def test_recovery_completes_matching_subset_and_preserves_other_runs(self):
        run = self.run_local(connected=True)
        archive = Archive(run.git)
        fresh = self.clone()
        restored = Archive(Git(fresh, run.id))
        git(fresh, "fetch", "-q", str(self.remote), archive.prefix + "run:" + archive.prefix + "run")
        git(fresh, "update-ref", "refs/gitweave/other/run", self.base)
        git(fresh, "update-ref", run.git.notes + "-other", self.base)
        restored.recover("origin")
        self.assertEqual(restored.local(), archive.local())
        self.assertEqual(git(fresh, "rev-parse", "refs/gitweave/other/run"), self.base)
        self.assertEqual(git(fresh, "rev-parse", run.git.notes + "-other"), self.base)

    def test_cli_transfer_error_reports_no_success_or_credentials(self):
        run = self.run_local()
        with patch("sys.argv", ["gitweave", "export", "--repo", str(self.repo), "--run", run.id,
                                 "--remote", "https://user:secret@example.test/repo"]), \
                patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(), 2)
            self.assertEqual(out.getvalue(), "")
            self.assertNotIn("secret", err.getvalue())

    def test_authentication_error_has_safe_specific_diagnostic(self):
        archive = Archive(Git(self.repo, "selected"))
        with patch.object(archive.git, "command", side_effect=Failure("git", "Authentication failed: secret")):
            with self.assertRaisesRegex(Failure, "authentication or permission denied") as caught:
                archive.remote(str(self.remote))
            self.assertNotIn("secret", str(caught.exception))
