"""Bounded wait for the page's own action-consistency report before Enter.

The creator chat page records an action locally and reports it with a
fire-and-forget POST to /aweme/v1/im/consistency/action/report. This module
observes only that existing response: it injects no script, sends no request
and never reads a body. Its single job is to let the caller give an in-flight
report a short, bounded chance to land before the message is dispatched.

Default OFF: two live tests showed the page never emits this report before Enter, so waiting only delayed the send. Set SPARKFLOW_ACTION_REPORT_GATE=1 to re-enable for experiments.
Timeout defaults to 4.0s, clamped to 0..10s.
"""
import asyncio
import os
import time

REPORT_PATH = '/aweme/v1/im/consistency/action/report'
_DEFAULT_TIMEOUT = 3.0
_MAX_TIMEOUT = 10.0
_OFF_VALUES = ('0', 'false', 'off', 'no')


def enabled():
    return str(os.environ.get('SPARKFLOW_ACTION_REPORT_GATE', '0')).strip().lower() not in _OFF_VALUES


def timeout_seconds():
    raw = os.environ.get('SPARKFLOW_ACTION_REPORT_GATE_TIMEOUT', '')
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    if value != value:  # NaN
        return _DEFAULT_TIMEOUT
    return min(max(value, 0.0), _MAX_TIMEOUT)


def _is_report(response):
    try:
        url = str(getattr(getattr(response, 'request', None), 'url', '') or '')
    except Exception:
        return False
    return url.split('?', 1)[0].split('#', 1)[0].endswith(REPORT_PATH)


class ActionReportGate:
    def __init__(self, page):
        self._closed = False
        self._context = None
        self._callback = None
        self._seen = 0
        self._last = None
        self._seen_at_mark = 0
        self._mark = None
        self.attach_error = None
        try:
            context = page.context
        except Exception as exc:
            self.attach_error = type(exc).__name__
            context = None
        if context is None:
            return

        def on_response(response):
            if self._closed:
                return
            if not _is_report(response):
                return
            self._seen += 1
            self._last = time.monotonic()

        try:
            context.on('response', on_response)
        except Exception as exc:
            self.attach_error = type(exc).__name__
            return
        self._context = context
        self._callback = on_response

    def begin_target(self):
        """Scope later checks to the target currently being opened."""
        self._mark = time.monotonic()
        self._seen_at_mark = self._seen

    def reports_since_mark(self):
        return max(0, self._seen - self._seen_at_mark)

    async def ensure_before_enter(self, timeout=None):
        if not enabled():
            return {'status': 'disabled', 'waited_ms': 0, 'reports': self.reports_since_mark()}
        if self._closed:
            return {'status': 'closed', 'waited_ms': 0, 'reports': self.reports_since_mark()}
        if self.reports_since_mark():
            return {'status': 'seen', 'waited_ms': 0, 'reports': self.reports_since_mark()}
        limit = timeout_seconds() if timeout is None else min(max(float(timeout), 0.0), _MAX_TIMEOUT)
        started = time.monotonic()
        deadline = started + limit
        while not self._closed and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            await asyncio.sleep(min(0.05, max(0.0, remaining)))
            if self.reports_since_mark():
                break
        status = 'seen' if self.reports_since_mark() else 'timeout'
        return {'status': status,
                'waited_ms': int((time.monotonic() - started) * 1000),
                'reports': self.reports_since_mark(),
                'attach_error': self.attach_error}

    def snapshot(self):
        return {'enabled': enabled(), 'timeout_seconds': timeout_seconds(),
                'seen_total': self._seen, 'reports_since_mark': self.reports_since_mark(),
                'closed': self._closed, 'attach_error': self.attach_error}

    def close(self):
        if self._closed:
            return
        self._closed = True
        context, callback = self._context, self._callback
        self._context = None
        self._callback = None
        if context is not None and callback is not None:
            try:
                context.remove_listener('response', callback)
            except Exception as exc:
                self.attach_error = type(exc).__name__


_CURRENT = None


def attach(page):
    global _CURRENT
    _CURRENT = ActionReportGate(page)
    return _CURRENT


def begin_target():
    """Mark the start of opening one target, before its conversation is clicked."""
    gate = _CURRENT
    if gate is not None:
        gate.begin_target()
