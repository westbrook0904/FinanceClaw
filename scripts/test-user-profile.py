#!/usr/bin/env python3
"""Local profile workflow checks; network boundary is stubbed, HTTP is covered by Java tests."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('profile_cli', Path(__file__).with_name('user-profile.py'))
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)
URL = 'http://localhost:8080'
TOKEN = 'test-token-only'
VERSION = '"profile-1-0-' + 'a' * 64 + '"'
UPDATED = '"profile-1-1-' + 'a' * 64 + '"'


class ProfileWorkflow(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.file = Path(self.folder.name) / 'profile.md'
        self.version = self.file.with_name('.profile.md.version.json')

    def pull(self, raw=b''):
        with patch.object(profile, 'exchange', return_value=(raw, VERSION)):
            profile.sync('pull', URL, TOKEN, self.file)

    def test_download_edit_upload_preserves_markdown_and_updates_version(self):
        self.pull('# 我的画像\r\n'.encode())
        raw = '# 我的画像\r\n- 宁波\n'.encode()
        self.file.write_bytes(raw)
        with patch.object(profile, 'exchange', return_value=(raw, UPDATED)) as exchange:
            profile.sync('push', URL, TOKEN, self.file)
            exchange.assert_called_once_with(URL, TOKEN, raw, VERSION)
        self.assertEqual(raw, self.file.read_bytes())
        self.assertEqual(UPDATED, json.loads(self.version.read_text())['etag'])
        self.assertNotIn(TOKEN, self.version.read_text())
        self.assertEqual(0o600, self.file.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.version.stat().st_mode & 0o777)

    def test_pull_does_not_clobber_unsaved_edits(self):
        self.pull()
        self.file.write_text('local edit')
        with patch.object(profile, 'exchange') as exchange:
            with self.assertRaisesRegex(profile.Invalid, 'local_file_exists'):
                profile.sync('pull', URL, TOKEN, self.file)
            exchange.assert_not_called()
        self.assertEqual('local edit', self.file.read_text())

    def test_conflict_preserves_file_and_old_version_for_manual_resolution(self):
        self.pull()
        self.file.write_text('local edit')
        with patch.object(profile, 'exchange', side_effect=profile.Invalid('http_409')):
            with self.assertRaisesRegex(profile.Invalid, 'http_409'):
                profile.sync('push', URL, TOKEN, self.file)
        self.assertEqual('local edit', self.file.read_text())
        self.assertEqual(VERSION, json.loads(self.version.read_text())['etag'])

    def test_empty_document_is_uploaded_as_put_not_get(self):
        self.pull(b'existing')
        self.file.write_bytes(b'')
        with patch.object(profile, 'exchange', return_value=(b'', UPDATED)) as exchange:
            profile.sync('push', URL, TOKEN, self.file)
            exchange.assert_called_once_with(URL, TOKEN, b'', VERSION)

    def test_local_file_without_version_cannot_be_uploaded(self):
        self.file.write_text('old unbound export')
        with self.assertRaisesRegex(profile.Invalid, 'pull_required'):
            profile.sync('push', URL, TOKEN, self.file)

    def test_version_is_bound_to_server(self):
        self.pull()
        with patch.object(profile, 'exchange') as exchange:
            with self.assertRaisesRegex(profile.Invalid, 'invalid_local_version'):
                profile.sync('push', 'https://other.example', TOKEN, self.file)
            exchange.assert_not_called()

    def test_oversized_or_invalid_utf8_upload_never_reaches_server(self):
        self.pull()
        for raw in [b'a' * 8193, b'\xff', b'private\x00content']:
            self.file.write_bytes(raw)
            with patch.object(profile, 'exchange') as exchange:
                with self.assertRaises(profile.Invalid):
                    profile.sync('push', URL, TOKEN, self.file)
                exchange.assert_not_called()

    def test_symlinked_profile_is_not_read_or_replaced(self):
        target = self.file.with_name('other.md')
        target.write_text('do not replace')
        self.file.symlink_to(target)
        with self.assertRaisesRegex(profile.Invalid, 'symlink_refused'):
            profile.sync('pull', URL, TOKEN, self.file, force=True)
        self.assertEqual('do not replace', target.read_text())

    def test_credentials_never_go_to_plaintext_remote_or_url_userinfo(self):
        for url in ['http://remote.example', 'https://user:pass@example.com',
                    'https://example.com?token=x', 'file:///tmp/profile']:
            with patch.object(profile, 'exchange') as exchange:
                with self.assertRaises(profile.Invalid):
                    profile.sync('pull', url, TOKEN, self.file)
                exchange.assert_not_called()

    def test_failure_after_document_download_leaves_no_uploadable_old_version(self):
        self.pull(b'old document')
        real_write = profile.write_file

        def fail_metadata(path, raw):
            if path == self.version:
                raise OSError('test write failure')
            real_write(path, raw)

        with patch.object(profile, 'exchange', return_value=(b'new document', UPDATED)), \
                patch.object(profile, 'write_file', side_effect=fail_metadata):
            with self.assertRaises(OSError):
                profile.sync('pull', URL, TOKEN, self.file, force=True)
        self.assertEqual(b'new document', self.file.read_bytes())
        self.assertFalse(self.version.exists())


if __name__ == '__main__':
    unittest.main()
