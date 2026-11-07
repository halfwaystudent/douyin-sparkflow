"""The host exporter can only inspect explicitly mapped containers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/export_host_status.py'


class ExportHostStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.calls = self.root / 'calls.jsonl'
        docker = self.root / 'docker'
        docker.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with Path(os.environ['CALLS']).open('a') as h:h.write(json.dumps(args)+'\\n')
if args[:4]!=['container','inspect','--format','{{.State.Status}}'] or len(args)!=5:sys.exit(90)
value=json.loads(os.environ['FAKE_STATES']).get(args[4],'missing')
if value=='missing':print('Error: No such container: '+args[4],file=sys.stderr);sys.exit(1)
if value=='denied':print('permission denied: sensitive internal detail',file=sys.stderr);sys.exit(1)
print(value)
''')
        docker.chmod(0o700)
        self.output = self.root / 'status/status.json'

    def export(self, states=None, extra=None):
        env = {**os.environ, 'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
               'CALLS': str(self.calls), 'FAKE_STATES': json.dumps(states or {})}
        return subprocess.run([sys.executable, str(SCRIPT), '--output', str(self.output),
                               '--service', 'douyin-web=spark-preview-web',
                               '--service', 'login-desktop=spark-preview-login', *(extra or [])],
                              text=True, capture_output=True, env=env, timeout=10)

    def test_exports_live_preview_services_and_explicit_undeployed_roles(self):
        result = self.export({'spark-preview-web': 'running', 'spark-preview-login': 'running'})
        self.assertEqual(0, result.returncode, result.stderr)
        snapshot = json.loads(self.output.read_text())
        self.assertEqual(1, snapshot['version'])
        rows = {row['Names']: row for row in snapshot['containers']}
        self.assertEqual('running', rows['douyin-web']['State'])
        self.assertEqual('running', rows['login-desktop']['State'])
        self.assertEqual('not_deployed', rows['douyin-scheduler']['State'])
        self.assertIn('未启用定时发送', rows['douyin-scheduler']['Status'])
        self.assertEqual('not_deployed', rows['mihomo']['State'])
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual({'spark-preview-web', 'spark-preview-login'}, {call[-1] for call in calls})
        self.assertEqual(2, len(calls))
        self.assertNotIn('spark-preview', self.output.read_text())
        self.assertEqual(0o644, self.output.stat().st_mode & 0o777)
        self.assertEqual([self.output], list(self.output.parent.iterdir()))

    def test_stopped_missing_and_unreadable_are_distinct_not_green(self):
        for state, expected in [('exited', 'exited'), ('missing', 'not_deployed'), ('denied', 'unavailable'), ('unexpected-sensitive-value', 'unavailable')]:
            with self.subTest(state=state):
                result = self.export({'spark-preview-web': state, 'spark-preview-login': 'running'})
                self.assertEqual(0, result.returncode, result.stderr)
                snapshot = self.output.read_text()
                row = next(row for row in json.loads(snapshot)['containers'] if row['Names'] == 'douyin-web')
                self.assertEqual(expected, row['State'])
                self.assertNotIn('sensitive', snapshot)

    def test_unknown_or_duplicate_roles_and_option_like_container_names_are_rejected(self):
        for mapping in ['unrelated=foo', 'douyin-web=other', 'mihomo=--privileged']:
            with self.subTest(mapping=mapping):
                result = self.export(extra=['--service', mapping])
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.calls.exists())

    def test_docker_timeout_or_missing_command_returns_unavailable(self):
        spec = importlib.util.spec_from_file_location('export_host_status', SCRIPT)
        exporter = importlib.util.module_from_spec(spec); spec.loader.exec_module(exporter)
        for error in [FileNotFoundError('offline'), subprocess.TimeoutExpired(['docker'], 3)]:
            with self.subTest(error=type(error).__name__), patch.object(exporter.subprocess, 'run', side_effect=error):
                self.assertEqual('unavailable', exporter.inspect_state('spark-preview-web'))
