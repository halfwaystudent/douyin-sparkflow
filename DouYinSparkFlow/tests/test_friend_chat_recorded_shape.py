"""Pin the guard's assumptions to the one recorded live page shape.

The fixture is sanitized structure only (class suffixes and ids replaced), so it
cannot prove the current page still looks like this. It does keep the depth and
selector assumptions from drifting silently, and it documents exactly what was
observed when the guard was adapted.
"""
import json
import unittest
from pathlib import Path

from core import friend_chat_guard

FIXTURE = Path(__file__).with_name('fixtures') / 'creator_center_identity_shape.json'


def load():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def provider_depths(node):
    return [fiber['depth'] for fiber in node['fiber']
            if any(path.startswith('value.conversation') for path in fiber['paths'])]


class RecordedShapeTests(unittest.TestCase):
    def test_fixture_is_sanitized(self):
        raw = FIXTURE.read_text(encoding='utf-8')
        self.assertNotIn('http', raw)
        for node in load()['after']['inputs']:
            self.assertEqual(node['attrs'], [])

    def test_header_and_input_reach_the_current_conversation_within_hop_limit(self):
        shape = load()['after']
        limit = int(friend_chat_guard._COMMON.split('depth<=', 1)[1].split(';', 1)[0])
        for kind in ('headers', 'inputs'):
            depths = [depth for node in shape[kind] for depth in provider_depths(node)]
            self.assertTrue(depths, kind)
            self.assertLessEqual(min(depths), limit, kind)

    def test_extra_header_tail_element_is_excluded_by_the_guard_filter(self):
        classes = [node['classes'] for node in load()['after']['headers']]
        self.assertEqual(len(classes), 2)
        kept = [c for c in classes
                if c.startswith('box-header-') and not c.startswith('box-header-tail-')]
        self.assertEqual(len(kept), 1)

    def test_rows_carry_no_identity_attributes_and_identity_is_one_level_up(self):
        for node in load()['after']['sampleRows']:
            self.assertEqual(node['attrs'], [])
            self.assertTrue(any(path.startswith('header.props.conversation')
                                for fiber in node['fiber'] for path in fiber['paths']))


if __name__ == '__main__':
    unittest.main()
