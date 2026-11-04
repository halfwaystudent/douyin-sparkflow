"""Personal literal messages win over global and holiday templates."""
from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import patch

from core import personal_streak
from core import msg_builder
from tests import test_shared_security as fixtures


class TemplateScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_settings_explains_that_personal_messages_override_global_templates(self):
        client=self.fixture.login('owner','Owner-secret-2026!')
        response=client.get('/settings')
        self.assertEqual(200,response.status_code)
        self.assertIn('已在“我的续火花”保存个人发送内容的用户，优先使用个人内容',response.text)

    def test_personal_message_does_not_expand_api_or_use_global_variants_and_holidays(self):
        principal={'username':'alice','role':'user'}
        messages=['[API] 保留原样','[data] 也是普通文字']
        personal_streak.save_preferences(principal,enabled=False,at_time='18:00',timezone_name='Asia/Shanghai',messages=messages)
        global_config={'messageTemplate':'global-fixed','sendStrategy':{'messageVariants':['global-variant']},'happyNewYear':{'enabled':True,'messageTemplate':'holiday-only'}}
        before=deepcopy(global_config)
        groups=personal_streak.account_groups(global_config,self.fixture.accounts,manual=True)
        self.assertEqual(1,len(groups))
        effective,accounts=groups[0]
        with patch.object(msg_builder,'request_hitokoto') as external, patch.object(msg_builder,'date') as clock:
            clock.today.return_value=date(2026,2,20)
            with msg_builder.using_message_config(effective):
                self.assertEqual(messages,msg_builder.build_message_candidates())
            external.assert_not_called()
        self.assertEqual(['a1'],[a['account_ref'] for a in accounts])
        self.assertEqual(before,global_config)

    def test_user_without_personal_content_cannot_fall_back_to_another_scope(self):
        groups=personal_streak.account_groups({'messageTemplate':'global-only'},self.fixture.accounts,manual=True)
        self.assertEqual([],groups)
