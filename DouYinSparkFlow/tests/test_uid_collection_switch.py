"""Isolation switch for the UID response listener (A/B test, no behaviour change by default)."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock
from core import friend_identity


class UidCollectionSwitchTests(unittest.TestCase):
    def page(self):
        page=Mock()
        page.on=Mock()
        return page

    def test_default_keeps_listening(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SPARKFLOW_UID_COLLECTION', None)
            page=self.page()
            collector=friend_identity.UserDetailCollector('synthetic').attach(page)
            page.on.assert_called_once()
            self.assertFalse(collector.summary().get('disabled'))

    def test_off_attaches_nothing_and_reads_no_body(self):
        with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION':'off'}):
            page=self.page()
            collector=friend_identity.UserDetailCollector('synthetic').attach(page)
            page.on.assert_not_called()
            self.assertTrue(collector.summary()['disabled'])
            # A stray callback must not start a body read either.
            response=Mock(url='https://example.invalid/aweme/v1/creator/im/user_detail/', json=AsyncMock())
            collector._on_response(response)
            response.json.assert_not_called()
            self.assertEqual(collector.entries(), [])

    def test_off_is_case_insensitive_and_accepts_false_like_values(self):
        for value in ['OFF','0','false','disabled','no']:
            with self.subTest(value=value), patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION':value}):
                self.assertTrue(friend_identity.uid_collection_enabled() is False)

    def test_unrelated_values_keep_collection_on(self):
        for value in ['','on','1','true','yes']:
            with self.subTest(value=value), patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION':value}):
                self.assertTrue(friend_identity.uid_collection_enabled() is True)

    def test_marker_wins_over_env_on_for_each_attach(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'uid-collection-off.flag'
            with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION': 'on'}), patch.object(
                friend_identity, 'uid_collection_marker_path', return_value=marker,
            ):
                first = self.page()
                collector = friend_identity.UserDetailCollector('synthetic').attach(first)
                first.on.assert_called_once()
                collector.detach()

                marker.touch()
                second = self.page()
                collector.attach(second)

            second.on.assert_not_called()
            self.assertTrue(collector.disabled)

    def test_drain_is_immediate_when_disabled(self):
        with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION':'off'}):
            collector=friend_identity.UserDetailCollector('synthetic').attach(self.page())
            async def run():
                await asyncio.wait_for(collector.drain(timeout=30), timeout=1)
            asyncio.run(run())


if __name__=='__main__':
    unittest.main()
