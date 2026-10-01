"""Recovery at Git boundaries, including real process termination."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gitweave.git import Git
from gitweave.model import Failure, Result
from gitweave.runtime import Runtime
from test_runtime import Fake, git, graph, node


# Runs in a separate process. The interrupted worker never returns a Result;
# killing the runtime also kills this thread, without leaving child processes.
CHILD = r'''
import json, sys, time
from pathlib import Path
from gitweave.model import Failure, Result
from gitweave.runtime import Runtime

repo, definition, state, stop = map(Path, sys.argv[1:])
selector = json.loads(stop.read_text())
class Adapter:
    def run(self, node, context, workspace, timeout):
        name = node['instruction']
        data = context['inputs'][0]['data']
        value = (data or 0) + 1 if name == 'inc' else None
        if name == 'flaky' and not (state / 'failed').exists():
            (state / 'failed').touch()
            raise Failure('provider', 'retry', retryable=True)
        if name == selector['node'] and ('item' not in selector or context['item'] == selector['item']) and ('value' not in selector or value == selector['value']):
            (workspace / 'aborted').write_text('unfinished files')
            (state / 'ready').write_text(json.dumps(dict(context=context, head=RuntimeGitHead(workspace))))
            while True:
                time.sleep(1)
        (workspace / name).write_text(str(context['item']))
        if name == 'plan':
            value = [0, 1, 2]
        elif name == 'route':
            value = True
        return Result(message=name, data=value, usage={'tokens': 7}, raw_stdout='saved log')
def RuntimeGitHead(workspace):
    import subprocess
    return subprocess.check_output(['git', '-C', str(workspace), 'rev-parse', 'HEAD'], text=True).strip()
run = Runtime(definition.read_text(), repo, 'HEAD', 'original request', adapters={'fake': Adapter()})
(state / 'id').write_text(run.id)
run.run()
'''


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
            'commit', '--allow-empty', '-qm', 'base')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.path = self.root / 'graph.json'

    def interrupt(self, nodes, flow, selector, completed, **options):
        self.path.write_text(graph(nodes, flow, **options))
        state = self.root / 'state'
        state.mkdir()
        stop = self.root / 'stop.json'
        stop.write_text(json.dumps(selector))
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        with subprocess.Popen([sys.executable, '-c', CHILD, str(self.repo), str(self.path), str(state), str(stop)],
                              env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail('Child exited before interruption: ' + process.communicate()[1])
                    if (state / 'ready').exists():
                        run_id = (state / 'id').read_text()
                        storage = Git(self.repo, run_id)
                        attempts = storage.load_attempts()
                        # Let completed siblings finish workspace cleanup before
                        # killing; the sole remaining worker is sleeping in Python.
                        worktrees = storage.command('worktree', 'list', '--porcelain').count('worktree ')
                        if sum(a['status'] == 'completed' for a in attempts) == completed and worktrees == 2:
                            break
                    time.sleep(.02)
                else:
                    self.fail('Timed out waiting for interrupted node and completed siblings')
            finally:
                process.kill()
                process.communicate(timeout=5)
        self.run_id = run_id
        self.storage = storage
        workspaces = [Path(line.removeprefix('worktree ')) for line in storage.command(
            'worktree', 'list', '--porcelain').splitlines() if line.startswith('worktree ')]
        def cleanup():
            for workspace in workspaces:
                if workspace != storage.repo and workspace.exists():
                    storage.remove_worktree(workspace)
                    workspace.parent.rmdir()
        self.addCleanup(cleanup)
        self.before = attempts
        self.ready = json.loads((state / 'ready').read_text())
        self.original = storage.load_run()
        self.old_refs = dict(line.split() for line in storage.command(
            'for-each-ref', '--format=%(refname) %(objectname)', f'refs/gitweave/{run_id}/').splitlines())
        self.old_notes = {a['commit']: storage.command('notes', f'--ref={storage.notes}', 'show', a['commit']) for a in attempts}
        self.assertEqual(self.original['status'], 'running')
        self.assertEqual(self.original['graph'], self.path.read_text())
        self.assertEqual(self.original['request'], 'original request')
        self.assertEqual(self.original['base_commit'], self.base)
        # Changing the source file must have no effect on resume.
        self.path.write_text('{invalid changed graph')

    def resume(self, work):
        run = Runtime.resume(self.run_id, self.repo, adapters={'fake': Fake(work)})
        record = run.run()
        self.assertEqual(record['status'], 'completed', record.get('failure'))
        self.assertEqual(record['run_id'], self.run_id)
        self.assertEqual(record['graph'], self.original['graph'])
        self.assertEqual(record['started_at'], self.original['started_at'])
        for ref, commit in self.old_refs.items():
            if not ref.endswith('/run'):
                self.assertEqual(self.storage.resolve(ref), commit)
        for commit, note in self.old_notes.items():
            self.assertEqual(self.storage.command('notes', f'--ref={self.storage.notes}', 'show', commit), note)
        for a in self.before:
            summary = next(s for s in record['attempts'] if s['instance_id'] == a['instance_id'] and s['attempt'] == a['attempt'])
            self.assertEqual(summary['status'], 'interrupted' if a['status'] == 'running' else a['status'])
        self.assertEqual(self.storage.load_run(), record)
        with self.assertRaisesRegex(Failure, 'already completed'):
            Runtime.resume(self.run_id, self.repo)
        return record

    def test_linear_restores_result_checkpoint_and_original_inputs(self):
        self.interrupt({'a': node('first'), 'b': node('halt'), 'c': node('last')},
                       ['a', 'b', 'c'], {'node': 'halt'}, 1, max_steps=3)
        seen = []
        def work(n, c, w):
            seen.append(n['instruction'])
            self.assertFalse((w / 'aborted').exists())
            self.assertEqual((w / 'first').read_text(), 'None')
            if n['instruction'] == 'halt':
                self.assertEqual(c, self.ready['context'])
                self.assertEqual(git(w, 'rev-parse', 'HEAD'), self.ready['head'])
                self.assertEqual(c['inputs'][0]['message'], 'first')
                (w / 'resumed').touch()
            else:
                self.assertTrue((w / 'resumed').exists())
            return Result(message='recovered')
        record = self.resume(work)
        self.assertEqual(seen, ['halt', 'last'])
        self.assertEqual(record['steps'], 3)
        self.assertEqual([a['attempt'] for a in record['attempts']], [1, 1, 2, 1])
        first = self.before[0]
        self.assertEqual(first['result']['usage'], {'tokens': 7})
        self.assertEqual(first['result']['raw_stdout'], 'saved log')

    def test_first_node_interruption_retains_definition_and_base(self):
        self.interrupt({'a': node('halt')}, ['a'], {'node': 'halt'}, 0, max_steps=1)
        record = self.resume(lambda *a: Result(message='done'))
        self.assertEqual(record['steps'], 1)
        self.assertEqual([a['status'] for a in record['attempts']], ['interrupted', 'completed'])

    def test_parallel_reuses_completed_sibling_even_when_it_finishes_later(self):
        self.interrupt({'a': node('done'), 'b': node('halt'), 'join': node('join')},
                       [{'parallel': [['b'], ['a']]}, 'join'], {'node': 'halt'}, 1, concurrency=2, max_steps=3)
        seen = []
        def work(n, c, w):
            seen.append(n['instruction'])
            if n['instruction'] == 'join':
                self.assertEqual([i['message'] for i in c['inputs']], ['rerun', 'done'])
                self.assertEqual(c['inputs'][1]['commit'], next(a['output_commit'] for a in self.before if a['status'] == 'completed'))
            return Result(message='rerun')
        record = self.resume(work)
        self.assertEqual(seen, ['halt', 'join'])
        self.assertEqual(record['steps'], 3)

    def test_partial_map_reuses_items_with_exhausted_remaining_budget(self):
        self.interrupt({'plan': node('plan', schema={'type': 'array'}), 'work': node('worker')},
                       ['plan', {'map': {'path': '/0/data', 'flow': ['work']}}],
                       {'node': 'worker', 'item': 1}, 3, concurrency=3, max_steps=4)
        seen = []
        def work(n, c, w):
            seen.append(c['item'])
            self.assertEqual(c, self.ready['context'])
            return Result(message='item 1')
        record = self.resume(work)
        self.assertEqual(seen, [1])
        self.assertEqual(record['steps'], 4)
        self.assertEqual([o['message'] for o in record['outputs']], ['worker', 'item 1', 'worker'])

    def test_map_pass_through_items_resume_without_consuming_steps(self):
        self.interrupt({'plan': node('plan', schema={'type': 'array'}),
                        'unused': node('unused'), 'halt': node('halt'), 'after': node('after')},
                       ['plan', {'map': {'path': '/0/data', 'flow': [
                           {'if': {'condition': {'path': '/0/data', 'equals': []},
                                   'then': ['unused'], 'else': []}}]}}, 'halt', 'after'],
                       {'node': 'halt'}, 1, max_steps=4)
        seen = []
        def work(n, c, w):
            seen.append(n['instruction'])
            if n['instruction'] == 'halt':
                self.assertEqual(c, self.ready['context'])
                self.assertEqual(len(c['inputs']), 3)
                self.assertTrue(all(i == c['inputs'][0] for i in c['inputs']))
                self.assertEqual(c['inputs'][0]['data'], [0, 1, 2])
                self.assertEqual(git(w, 'rev-parse', 'HEAD'), self.ready['head'])
            return Result(message=n['instruction'])
        record = self.resume(work)
        self.assertEqual(seen, ['halt', 'after'])
        self.assertEqual(record['steps'], 3)
        self.assertEqual([a['attempt'] for a in record['attempts']], [1, 1, 2, 1])

    def test_native_fetch_restores_interrupted_run_in_a_new_repository(self):
        self.interrupt({'a': node('done'), 'b': node('halt')}, ['a', 'b'], {'node': 'halt'}, 1)
        source = self.repo
        self.repo = self.root / 'restored'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        prefix = f'refs/gitweave/{self.run_id}/'
        git(self.repo, 'fetch', '-q', str(source), prefix + '*:' + prefix + '*',
            self.storage.notes + ':' + self.storage.notes)
        # An offline destination must stay offline after moving the Run, even if
        # the new repository happens to have an origin configured.
        git(self.repo, 'remote', 'add', 'origin', str(self.root / 'missing-remote'))
        self.storage = Git(self.repo, self.run_id)
        calls = []
        def work(n, c, w):
            calls.append(n['instruction'])
            self.assertEqual((w / 'done').read_text(), 'None')
            self.assertEqual(c, self.ready['context'])
            return Result(message='restored')
        record = self.resume(work)
        self.assertEqual(calls, ['halt'])
        self.assertEqual(record['repository'], str(self.repo.resolve()))
        self.assertIsNone(record['provenance_destination'])

    def test_loop_reuses_earlier_iterations_and_continues_at_interruption(self):
        self.interrupt({'inc': node('inc', schema={'type': 'integer'})},
                       [{'loop': {'flow': ['inc'], 'while': {'path': '/0/data', 'equals': 1}}},
                        {'loop': {'flow': ['inc'], 'while': {'path': '/0/data', 'equals': 3}}}],
                       {'node': 'inc', 'value': 3}, 2, max_steps=5)
        values = []
        def work(n, c, w):
            value = c['inputs'][0]['data'] + 1
            values.append(value)
            return Result(data=value)
        record = self.resume(work)
        self.assertEqual(values, [3, 4])
        self.assertEqual(record['outputs'][0]['data'], 4)
        ids = [a['invocation_id'] for a in record['attempts'] if a['status'] == 'completed']
        self.assertEqual(ids, ['flow/0/loop/0/0/inc', 'flow/0/loop/1/0/inc',
                               'flow/1/loop/0/0/inc', 'flow/1/loop/1/0/inc'])

    def test_nested_if_parallel_map_loop_and_retries(self):
        condition = {'path': '/0/data', 'equals': True}
        self.interrupt({'route': node('route', schema={'type': 'boolean'}),
                        'plan': node('plan', schema={'type': 'array'}), 'work': node('worker', schema={'type': 'null'}),
                        'flaky': node('flaky'), 'bad': node('unselected')},
                       ['route', {'if': {'condition': condition, 'then': [
                           {'parallel': [['plan', {'map': {'path': '/0/data', 'flow': [
                               {'loop': {'flow': ['work'], 'while': {'path': '/0/data', 'equals': True}}}]}}], ['flaky']]}],
                           'else': ['bad']}}],
                       {'node': 'flaky'}, 5, concurrency=4, retries=1)
        calls = []
        def work(n, c, w):
            calls.append(n['instruction'])
            if len(calls) == 1:
                raise Failure('provider', 'retry again', retryable=True)
            return Result(message='done')
        record = self.resume(work)
        self.assertEqual(calls, ['flaky', 'flaky'])
        attempts = [a for a in record['attempts'] if a['instance_id'].startswith('flaky-')]
        self.assertEqual([a['attempt'] for a in attempts], [1, 2, 3, 4])
        self.assertEqual([a['status'] for a in attempts], ['failed', 'interrupted', 'failed', 'completed'])
        self.assertEqual(len({a['invocation_id'] for a in attempts}), 1)
        self.assertEqual(record['steps'], 6)

    def test_repeated_resumes_cannot_bypass_logical_step_limit(self):
        calls = []
        run = Runtime(graph({'a': node(schema={'type': 'boolean'})}, [
            {'loop': {'flow': ['a'], 'while': {'path': '/0/data', 'equals': True}}}], max_steps=2),
            self.repo, self.base, adapters={'fake': Fake(lambda *a: (calls.append(1), Result(data=True))[1])})
        self.assertEqual(run.run()['failure']['kind'], 'step_limit')
        for _ in range(2):
            record = Runtime.resume(run.id, self.repo, adapters={'fake': Fake(lambda *a: self.fail('Reexecuted completed node'))}).run()
            self.assertEqual(record['failure']['kind'], 'step_limit')
            self.assertEqual(record['steps'], 2)
            self.assertEqual(len(record['attempts']), 2)
        self.assertEqual(len(calls), 2)

    def test_stable_ids_ignore_sibling_scheduling_order(self):
        definition = graph({'a': node()}, [{'parallel': [['a', 'a'], ['a', 'a']]}], concurrency=2)
        identities = []
        for delayed in (0, 1):
            barrier = threading.Barrier(2)
            def work(n, c, w):
                barrier.wait(timeout=5)
                return Result(message='done')
            run = Runtime(definition, self.repo, self.base, adapters={'fake': Fake(work)})
            original = run.node
            async def reordered(name, inputs, item, origin, invocation):
                if invocation.startswith(f'flow/0/parallel/{delayed}/'):
                    import asyncio
                    await asyncio.sleep(.03)
                return await original(name, inputs, item, origin, invocation)
            with patch.object(run, 'node', side_effect=reordered):
                record = run.run()
            self.assertEqual(record['status'], 'completed', record.get('failure'))
            identities.append({a['invocation_id']: a['instance_id'] for a in record['attempts']})
        self.assertEqual(identities[0].keys(), identities[1].keys())
        self.assertNotEqual(identities[0], identities[1])

    def test_discovery_in_current_repository_and_shared_store(self):
        run = Runtime(graph({'a': node()}, ['a']), self.repo, self.base)
        run.record['provenance_destination'] = None
        run.git.run_record(run.record)
        with contextlib.chdir(self.repo):
            self.assertEqual(Runtime.resume(run.id).id, run.id)
        store = self.root / '.gitweave' / 'repos' / 'owner' / 'repo.git'
        store.parent.mkdir(parents=True)
        git(self.repo, 'clone', '--bare', '-q', str(self.repo), str(store))
        git(store, 'fetch', '-q', str(self.repo), f'refs/gitweave/{run.id}/run:refs/gitweave/{run.id}/run')
        with contextlib.chdir(self.root):
            self.assertEqual(Runtime.resume(run.id).git.repo, store.resolve())

    def test_invalid_legacy_missing_and_corrupt_runs_are_rejected(self):
        for run_id in ('../unsafe', 'missing'):
            with self.assertRaises(Failure):
                Runtime.resume(run_id, self.repo)
        run = Runtime(graph({'a': node()}, ['a']), self.repo, self.base)
        run.record['version'] = 1
        run.git.run_record(run.record)
        with self.assertRaisesRegex(Failure, 'predates resume'):
            Runtime.resume(run.id, self.repo)
        run.record.update(version=2, graph_digest='wrong')
        run.git.run_record(run.record)
        with self.assertRaisesRegex(Failure, 'digest'):
            Runtime.resume(run.id, self.repo)

    def test_run_lock_prevents_concurrent_resume(self):
        run = Runtime(graph({'a': node()}, ['a']), self.repo, self.base)
        run.git.run_record(run.record)
        resume = Runtime.resume(run.id, self.repo)
        with run.git.run_lock(), self.assertRaisesRegex(Failure, 'already executing'):
            resume.run()

    def test_saved_else_branch_and_github_identity_are_restored_without_fetch(self):
        nodes = {'route': node('route', schema={'type': 'boolean'}),
                 'then': node('unselected'), 'else': node('selected')}
        seen = []
        def initial(n, c, w):
            if n['instruction'] == 'route':
                return Result(data=False)
            raise Failure('provider', 'failed')
        run = Runtime(graph(nodes, ['route', {'if': {
            'condition': {'path': '/0/data', 'equals': True}, 'then': ['then'], 'else': ['else']}}]),
            self.repo, self.base, 'guidance', adapters={'fake': Fake(initial)})
        # Model a resolved Issue Run without involving a network or credentials.
        run.github_repository = run.record['github_repository'] = 'owner/repo'
        run.run_input = run.record['run_input'] = {'kind': 'issue', 'number': 108}
        with patch('gitweave.runtime.destination', return_value=None):
            self.assertEqual(run.run()['status'], 'failed')
        def work(n, c, w):
            seen.append(n['instruction'])
            self.assertEqual(c['github_repository'], 'owner/repo')
            self.assertEqual(c['run_input'], {'kind': 'issue', 'number': 108})
            self.assertEqual(c['request'], 'guidance')
            return Result(message='done')
        resumed = Runtime.resume(run.id, self.repo, adapters={'fake': Fake(work)})
        original = resumed.git.command
        def command(*args, **kwargs):
            self.assertNotEqual(args[0], 'fetch')
            return original(*args, **kwargs)
        with patch.object(resumed.git, 'command', side_effect=command):
            record = resumed.run()
        self.assertEqual(record['status'], 'completed', record.get('failure'))
        self.assertEqual(seen, ['selected'])
        self.assertEqual(record['attempts'][-1]['invocation_id'], 'flow/1/if/else/0/else')

    def test_cli_resumes_command_run_and_rejects_completed_run_without_changes(self):
        command = {'kind': 'command', 'argv': [sys.executable, '-c',
                   'import json; print(json.dumps({"message": "done", "data": True}))'],
                   'schema': {'type': 'boolean'}}
        run = Runtime(graph({'command': command}, ['command']), self.repo, self.base)
        run.git.run_record(run.record)
        args = [sys.executable, '-m', 'gitweave', 'resume', '--run', run.id, '--repo', str(self.repo)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record['run_id'], run.id)
        self.assertTrue(record['outputs'][0]['data'])
        self.assertEqual([json.loads(line)['type'] for line in result.stderr.splitlines()],
                         ['node_started', 'node_output', 'node_completed'])
        refs = run.git.command('for-each-ref', f'refs/gitweave/{run.id}/', run.git.notes)
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('already completed', result.stderr)
        self.assertEqual(run.git.command('for-each-ref', f'refs/gitweave/{run.id}/', run.git.notes), refs)
