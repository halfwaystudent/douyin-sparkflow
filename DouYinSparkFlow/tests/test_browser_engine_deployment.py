"""Optional deployment example stays off and shares only readonly engine inputs."""
from pathlib import Path
import unittest
import json

ROOT=Path(__file__).resolve().parents[2]

class OptionalEngineDeploymentTests(unittest.TestCase):
    def overlay(self):
        path=ROOT/'docker-compose.fingerprint.example.yml'
        self.assertTrue(path.is_file(),'default-off optional-engine example is missing')
        return json.loads(path.read_text())['services']

    def test_every_browser_consumer_gets_the_same_default_off_policy(self):
        services=self.overlay()
        self.assertEqual({'web','login-desktop','scheduler','task'},set(services))
        for service in services.values():
            env=service['environment']
            self.assertEqual('${SPARKFLOW_FINGERPRINT_BROWSER_ENABLED:-0}',env['SPARKFLOW_FINGERPRINT_BROWSER_ENABLED'])
            self.assertEqual('/run/browser-engine/policy.json',env['SPARKFLOW_BROWSER_ENGINE_POLICY_FILE'])

    def test_policy_and_binary_inputs_are_shared_readonly(self):
        for service in self.overlay().values():
            self.assertEqual({'./state/browser-engine:/run/browser-engine:ro','./state/browser-binaries:/opt/sparkflow-browser-binaries:ro'},set(service['volumes']))

    def test_overlay_does_not_change_process_network_or_privileges(self):
        forbidden={'image','command','entrypoint','ports','network_mode','privileged','cap_add','devices'}
        for service in self.overlay().values():
            self.assertFalse(set(service)&forbidden)
            self.assertFalse(any('docker.sock' in x for x in service['volumes']))

if __name__=='__main__':unittest.main()
