import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'runtime_installer', Path(__file__).resolve().parents[1] / 'scripts/install-runtime.py'
)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeInstallerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = self.root / 'runtime.json'
        self.manifest.write_text(json.dumps({
            'version': '1.2.3', 'release_url': 'https://example.invalid/v{version}',
            'archive': 'rtc_{version}_{arch}.tar.gz', 'executable': 'upstream-server',
        }))
        self.payload = b'#!/bin/sh\nprintf "Signaling runtime 1.2.3\\n"\n'
        self.archive = self.root / 'fixture.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as archive:
            member = tarfile.TarInfo('upstream-server')
            member.size = len(self.payload)
            archive.addfile(member, io.BytesIO(self.payload))
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def download(self, url, destination):
        if url.endswith('/checksums.txt'):
            destination.write_text(f'{self.digest}  rtc_1.2.3_amd64.tar.gz\n')
        else:
            self.assertEqual(url, 'https://example.invalid/v1.2.3/rtc_1.2.3_amd64.tar.gz')
            shutil.copyfile(self.archive, destination)

    def test_installs_verified_executable_and_skips_repeat_download(self):
        with patch.object(runtime, 'download', side_effect=self.download) as download:
            runtime.install(self.root, 'amd64', self.manifest)
            target = self.root / '.tools/rtc-server'
            self.assertEqual(target.read_bytes(), self.payload)
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            runtime.install(self.root, 'amd64', self.manifest)
            self.assertEqual(download.call_count, 2)

    def test_bad_checksum_preserves_existing_executable(self):
        target = self.root / '.tools/rtc-server'
        target.parent.mkdir()
        target.write_text('#!/bin/sh\necho "old version"\n')
        target.chmod(0o755)
        previous = target.read_bytes()
        self.digest = '0' * 64
        with patch.object(runtime, 'download', side_effect=self.download):
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                runtime.install(self.root, 'amd64', self.manifest)
        self.assertEqual(target.read_bytes(), previous)
        self.assertFalse(target.with_suffix('.pending').exists())


if __name__ == '__main__':
    unittest.main()
