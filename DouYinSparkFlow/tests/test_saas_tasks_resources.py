import unittest
from core import tasks

class ResourceRoutingTests(unittest.TestCase):
    def test_resource_pressure_is_not_a_network_or_login_failure(self):
        class ResourcePressure(RuntimeError):
            category='browser_resource_unavailable'
        for stage in ['open_chat_page','send_flow','friend_list']:
            with self.subTest(stage=stage):
                self.assertEqual(tasks.classify_browser_failure(stage,ResourcePressure('insufficient memory')),'browser_resource_unavailable')
        self.assertIn('browser_resource_unavailable',tasks.ACCOUNT_LEVEL_FAILURE_CATEGORIES)

if __name__=='__main__':unittest.main()
