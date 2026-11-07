"""Bounded lifecycle behavior for passive UID response collection."""
import asyncio
import os
import unittest
from unittest.mock import patch

from core import friend_identity


USER_DETAIL_URL = 'https://example.invalid/aweme/v1/creator/im/user_detail/'


class FakePage:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers.get(event, []).remove(handler)

    def emit(self, event, response):
        for handler in list(self.handlers.get(event, [])):
            handler(response)


class HangingResponse:
    url = USER_DETAIL_URL

    def __init__(self):
        self.json_started = asyncio.Event()
        self.json_cancelled = asyncio.Event()
        self.text_calls = 0

    async def json(self):
        self.json_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.json_cancelled.set()
            raise

    async def text(self):
        self.text_calls += 1
        return '{}'


class DelayedResponse:
    url = USER_DETAIL_URL

    def __init__(self, release, activity):
        self.release = release
        self.activity = activity

    async def json(self):
        self.activity['active'] += 1
        self.activity['max_active'] = max(self.activity['max_active'], self.activity['active'])
        try:
            await self.release.wait()
        finally:
            self.activity['active'] -= 1


class JsonFailsThenTextHangsResponse:
    url = USER_DETAIL_URL

    def __init__(self):
        self.json_calls = 0
        self.text_calls = 0
        self.text_started = asyncio.Event()

    async def json(self):
        self.json_calls += 1
        raise ValueError('synthetic decode failure')

    async def text(self):
        self.text_calls += 1
        self.text_started.set()
        await asyncio.Event().wait()


class ImmediateResponse:
    url = USER_DETAIL_URL

    def __init__(self, uid):
        self.uid = uid
        self.json_calls = 0

    async def json(self):
        self.json_calls += 1
        return {'status_code': 0, 'user_list': [
            {'user_id': self.uid, 'user': {'nickname': 'Sample'}}
        ]}


class DefersCancellationResponse(ImmediateResponse):
    def __init__(self):
        super().__init__('deferred-synthetic')
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def json(self):
        self.json_calls += 1
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
        return {'status_code': 0, 'user_list': [
            {'user_id': self.uid, 'user': {'nickname': 'Sample'}}
        ]}


class UserDetailCollectorLifecycleTests(unittest.TestCase):
    def test_aclose_cancels_a_hanging_body_read(self):
        async def run():
            page = FakePage()
            collector = friend_identity.UserDetailCollector('synthetic').attach(page)
            response = HangingResponse()
            page.emit('response', response)
            await asyncio.wait_for(response.json_started.wait(), timeout=0.2)
            await asyncio.wait_for(collector.aclose(), timeout=0.2)
            return collector, page, response

        with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION': 'on'}):
            collector, page, response = asyncio.run(run())

        self.assertTrue(response.json_cancelled.is_set())
        self.assertEqual([], page.handlers['response'])
        self.assertEqual([], collector.entries())
        self.assertEqual(0, response.text_calls)

    def test_response_flood_caps_pending_tasks_and_body_reads(self):
        async def run():
            page = FakePage()
            collector = friend_identity.UserDetailCollector('synthetic').attach(page)
            release = asyncio.Event()
            activity = {'active': 0, 'max_active': 0}
            for _ in range(friend_identity.MAX_PENDING_BODY_READS + 5):
                page.emit('response', DelayedResponse(release, activity))
            await asyncio.sleep(0)
            pending_count = len(collector._tasks)
            max_active = activity['max_active']
            await collector.aclose()
            return pending_count, max_active

        with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION': 'on'}):
            pending_count, max_active = asyncio.run(run())

        self.assertLessEqual(pending_count, friend_identity.MAX_PENDING_BODY_READS)
        self.assertLessEqual(max_active, friend_identity.MAX_CONCURRENT_BODY_READS)

    def test_body_read_has_one_total_timeout_across_json_and_text(self):
        async def run():
            page = FakePage()
            collector = friend_identity.UserDetailCollector('synthetic').attach(page)
            response = JsonFailsThenTextHangsResponse()
            page.emit('response', response)
            await asyncio.wait_for(response.text_started.wait(), timeout=0.2)
            await asyncio.sleep(friend_identity.BODY_READ_TIMEOUT_SECONDS * 2)
            pending_count = len(collector._tasks)
            await collector.aclose()
            return response, collector, pending_count

        with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION': 'on'}), patch.object(
            friend_identity, 'BODY_READ_TIMEOUT_SECONDS', 0.01,
        ):
            response, collector, pending_count = asyncio.run(run())

        self.assertEqual(1, response.json_calls)
        self.assertEqual(1, response.text_calls)
        self.assertEqual(0, pending_count)
        self.assertIn('body_read_timeout', collector.errors)

    def test_late_callback_is_ignored_after_detach_and_reuse(self):
        async def run():
            first_page = FakePage()
            collector = friend_identity.UserDetailCollector('synthetic').attach(first_page)
            stale_listener = first_page.handlers['response'][0]
            collector.detach()

            stale_response = ImmediateResponse('stale-synthetic')
            stale_listener(stale_response)
            await asyncio.sleep(0)

            second_page = FakePage()
            collector.attach(second_page)
            stale_listener(stale_response)
            fresh_response = ImmediateResponse('fresh-synthetic')
            second_page.emit('response', fresh_response)
            await collector.drain()
            return collector, stale_response, fresh_response

        with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION': 'on'}):
            collector, stale_response, fresh_response = asyncio.run(run())

        self.assertEqual(0, stale_response.json_calls)
        self.assertEqual(1, fresh_response.json_calls)
        self.assertEqual(['fresh-synthetic'], [entry['uid'] for entry in collector.entries()])

    def test_aclose_is_bounded_when_body_defers_cancellation(self):
        async def run():
            page = FakePage()
            collector = friend_identity.UserDetailCollector('synthetic').attach(page)
            response = DefersCancellationResponse()
            page.emit('response', response)
            await asyncio.wait_for(response.started.wait(), timeout=0.2)
            close_task = asyncio.create_task(collector.aclose())
            try:
                await asyncio.wait_for(response.cancelled.wait(), timeout=0.2)
                await asyncio.sleep(friend_identity.CLOSE_WAIT_SECONDS * 2)
                close_finished = close_task.done()
            finally:
                response.release.set()
            await asyncio.wait_for(close_task, timeout=0.2)
            return collector, close_finished

        with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION': 'on'}), patch.object(
            friend_identity, 'CLOSE_WAIT_SECONDS', 0.01,
        ):
            collector, close_finished = asyncio.run(run())

        self.assertTrue(close_finished)
        self.assertEqual([], collector.entries())

    def test_timeout_finishes_worker_without_ingesting_a_late_body(self):
        async def run():
            page = FakePage()
            collector = friend_identity.UserDetailCollector('synthetic').attach(page)
            response = DefersCancellationResponse()
            page.emit('response', response)
            await asyncio.wait_for(response.started.wait(), .2)
            try:
                await asyncio.wait_for(response.cancelled.wait(), .2)
                await asyncio.sleep(.03)
                self.assertEqual(0, len(collector._tasks), 'reader worker must honor its deadline')
                self.assertTrue(collector.pending_tasks(), 'uncooperative body must remain tracked')
                response.release.set()
                await collector.drain(.2)
                await asyncio.sleep(.01)
                self.assertEqual([], collector.entries(), 'a late body must not become new identity data')
            finally:
                response.release.set()
                await collector.aclose()
        with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION': 'on'}), patch.object(
            friend_identity, 'BODY_READ_TIMEOUT_SECONDS', .01,
        ):
            asyncio.run(run())

    def test_aclose_reports_unfinished_body_instead_of_claiming_cleanup(self):
        async def run():
            collector = friend_identity.UserDetailCollector('synthetic').attach(FakePage())
            response = DefersCancellationResponse()
            collector._page.emit('response', response)
            await asyncio.wait_for(response.started.wait(), .2)
            try:
                self.assertFalse(await collector.aclose())
                self.assertTrue(collector.pending_tasks())
            finally:
                response.release.set()
                pending = collector.pending_tasks()
                if pending:
                    await asyncio.wait(pending, timeout=.2)
            self.assertTrue(await collector.aclose())
        with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION': 'on'}), patch.object(
            friend_identity, 'CLOSE_WAIT_SECONDS', .01,
        ):
            asyncio.run(run())

    def test_queued_reader_deadline_expires_without_starting_its_body(self):
        async def run():
            collector = friend_identity.UserDetailCollector('synthetic').attach(FakePage())
            blockers = [DefersCancellationResponse() for _ in range(friend_identity.MAX_CONCURRENT_BODY_READS)]
            for response in blockers:
                collector._page.emit('response', response)
            await asyncio.gather(*(asyncio.wait_for(r.started.wait(), .2) for r in blockers))
            quick = ImmediateResponse('queued-synthetic')
            collector._page.emit('response', quick)
            try:
                await asyncio.sleep(.05)
                self.assertEqual(quick.json_calls, 0)
                self.assertFalse(collector._tasks, 'queued supervisor must not wait indefinitely for a slot')
                self.assertTrue(collector.pending_tasks(), 'uncooperative bodies are still owned')
            finally:
                for response in blockers:
                    response.release.set()
                await collector.aclose()
        with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION': 'on'}), patch.object(
            friend_identity, 'BODY_READ_TIMEOUT_SECONDS', .01,
        ):
            asyncio.run(run())

    def test_unexpected_reader_exception_is_consumed_after_detach(self):
        async def run():
            collector = friend_identity.UserDetailCollector('synthetic').attach(FakePage())
            errors = []
            loop = asyncio.get_running_loop()
            old = loop.get_exception_handler()
            loop.set_exception_handler(lambda _loop, context: errors.append(context))
            try:
                with patch.object(collector, 'ingest', side_effect=RuntimeError('synthetic ingestion error')):
                    collector._page.emit('response', ImmediateResponse('synthetic-id'))
                    await collector.drain(.2)
                    await collector.aclose()
                await asyncio.sleep(0)
                self.assertEqual(errors, [])
            finally:
                loop.set_exception_handler(old)
        with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION': 'on'}):
            asyncio.run(run())

    def test_sync_detach_cancels_mid_read_without_waiting(self):
        async def run():
            page = FakePage()
            collector = friend_identity.UserDetailCollector('synthetic').attach(page)
            response = HangingResponse()
            page.emit('response', response)
            await asyncio.wait_for(response.json_started.wait(), timeout=0.2)
            collector.detach()
            await asyncio.wait_for(response.json_cancelled.wait(), timeout=0.2)
            await collector.drain(timeout=0.2)
            return collector, page

        with patch.dict(os.environ, {'SPARKFLOW_UID_COLLECTION': 'on'}):
            collector, page = asyncio.run(run())

        self.assertEqual([], page.handlers['response'])
        self.assertEqual([], collector.entries())


if __name__ == '__main__':
    unittest.main()
