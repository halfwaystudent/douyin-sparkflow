"""A list-only refresh cannot reconnect an outbound WebSocket sender."""
import asyncio
import unittest
from unittest.mock import AsyncMock
from tests import test_friend_refresh_completeness as fixture

class RefreshSocketTests(unittest.TestCase):
    def test_readonly_refresh_installs_socket_denial_before_returning(self):
        context=fixture._StubContext()
        fixture.PersistentProfileRefreshTests()._run(fixture.PROFILE_CONFIG,context,[])
        context.route_web_socket.assert_awaited_once()
        pattern,handler=context.route_web_socket.await_args.args
        self.assertEqual(pattern,'**/*')
        ws=AsyncMock()
        asyncio.run(handler(ws))
        ws.close.assert_awaited_once()

if __name__=='__main__':unittest.main()
