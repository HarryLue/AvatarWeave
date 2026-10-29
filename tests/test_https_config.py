import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('https_config', Path(__file__).resolve().parents[1] / 'scripts/configure-https.py')
https = importlib.util.module_from_spec(spec)
spec.loader.exec_module(https)


class HTTPSConfigTest(unittest.TestCase):
    def test_ip_uses_short_lived_profile_and_webroot(self):
        command = https.issue_command({'hosts':['111.56.189.4'],'certificate_name':'avatarweave','email':''})
        self.assertIn('--ip-address', command)
        self.assertIn('shortlived', command)
        self.assertIn('--webroot', command)
        self.assertIn('--keep-until-expiring', command)
        self.assertIn('--renew-with-new-domains', command)

    def test_domain_changes_only_configuration(self):
        command = https.issue_command({'hosts':['avatar.example.com'],'certificate_name':'avatarweave','email':'ops@example.com'})
        self.assertIn('--domain', command)
        self.assertIn('--email', command)
        self.assertNotIn('--ip-address', command)
        self.assertNotIn('--preferred-profile', command)

    def test_rejects_nginx_injection_and_private_hosts(self):
        for host in ['example.com; return 200','127.0.0.1','localhost','/etc/passwd']:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / 'https.json'
                config.write_text(json.dumps({'hosts':[host],'certificate_name':'avatarweave'}))
                with self.assertRaises(ValueError): https.load_config(config)

    def test_nginx_failure_restores_previous_config(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / 'site.conf'
            site.write_text('previous configuration')
            with patch.object(https,'SITE',site), patch.object(https,'reload_nginx',side_effect=RuntimeError('test failure')):
                with self.assertRaises(RuntimeError):
                    https.enable_site({'hosts':['example.com']},Path('/cert.pem'),Path('/key.pem'))
            self.assertEqual(site.read_text(),'previous configuration')

    def test_ca_failure_keeps_valid_public_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'infra').mkdir()
            (root / 'infra/https.json').write_text(json.dumps({
                'hosts':['111.56.189.4'], 'certificate_name':'avatarweave',
                'fallback_self_signed':True,
            }))
            lineage = root / 'public/avatarweave'
            lineage.mkdir(parents=True)
            (lineage / 'privkey.pem').touch()
            with patch.object(https,'ROOT',root), patch.object(https,'CERTIFICATE_ROOT',root/'public'), patch.object(https,'ACME_ROOT',root/'acme'), patch.object(https.subprocess,'run',return_value=subprocess.CompletedProcess([],1)), patch.object(https,'certificate_valid',return_value=True), patch.object(https,'self_signed') as fallback, patch.object(https,'enable_site') as enable:
                https.configure(renew=True)
                fallback.assert_not_called()
                enable.assert_called_once_with(unittest.mock.ANY, lineage/'fullchain.pem', lineage/'privkey.pem')


if __name__ == '__main__': unittest.main()
