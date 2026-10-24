"""Douyin's binary message_send receipt.

The fixtures are rebuilt from the field layout of live receipts logged on the
production server (ids and text replaced): an accepted send and a send refused
with status_code 8101 inside check_message.
"""

import json
import unittest

from core import im_receipt, streak_state


def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def key(field, wire):
    return varint((field << 3) | wire)


def vfield(field, value):
    return key(field, 0) + varint(value)


def bfield(field, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return key(field, 2) + varint(len(data)) + data


SERVER_MSG_ID = 7564000000000000123
CLIENT_MSG_ID = "c97ebf0a-0000-4000-8000-000000000000"
LOG_ID = "20261024134342000000000000000000AB"


def send_body(*, msg_id=SERVER_MSG_ID, status=0, check_code=0, check_message=None):
    body = b""
    if msg_id is not None:
        body += vfield(1, msg_id)
    body += vfield(3, status)
    body += bfield(4, CLIENT_MSG_ID)
    body += vfield(5, check_code)
    if check_message is not None:
        body += bfield(6, check_message)
    return body


def response(*, cmd=100, status_code=0, error_desc="OK", body=None, trailing=True):
    out = vfield(1, cmd) + vfield(2, 10066) + vfield(3, status_code) + bfield(4, error_desc) + vfield(5, 0)
    if body is not None:
        out += bfield(6, bfield(100, body))
    out += bfield(7, LOG_ID)
    if trailing:
        out += vfield(10, 1761284622) + vfield(11, 1761284622) + vfield(13, 5000000)
    return out


ACCEPTED = response(body=send_body())
REFUSED_8101 = response(
    body=send_body(
        check_message=json.dumps(
            {"status_code": 8101, "tips": "", "status_msg": {"msg_type": 2}, "raw_check_code": 1}
        )
    )
)


class ParseSendResponseTests(unittest.TestCase):
    def test_live_shaped_accepted_receipt_is_ok(self):
        result = im_receipt.parse_send_response(ACCEPTED)

        self.assertTrue(result["ok"])
        self.assertEqual(100, result["cmd"])
        self.assertEqual(0, result["statusCode"])
        self.assertEqual("OK", result["errorDesc"])
        self.assertTrue(result["hasServerMessageId"])
        self.assertEqual(LOG_ID, result["logId"])

    def test_refusal_inside_check_message_is_not_ok_even_with_a_message_id(self):
        # The live 8101 refusal still carries a server message id and
        # check_code 0; only the embedded JSON says no.
        result = im_receipt.parse_send_response(REFUSED_8101)

        self.assertFalse(result["ok"])
        self.assertEqual({"status_code": 8101, "raw_check_code": 1}, result["checkCodes"])

    def test_every_non_zero_code_or_missing_piece_is_not_ok(self):
        cases = {
            "outer status": response(status_code=7, body=send_body()),
            "send status": response(body=send_body(status=3)),
            "check code": response(body=send_body(check_code=1)),
            "no message id": response(body=send_body(msg_id=None)),
            "zero message id": response(body=send_body(msg_id=0)),
            "no send body": response(body=None),
            "other command": response(cmd=200, body=send_body()),
            "check_message not json": response(body=send_body(check_message='x "status_code":8101 y')),
        }
        for name, raw in cases.items():
            with self.subTest(name):
                result = im_receipt.parse_send_response(raw)
                self.assertIsNotNone(result)
                self.assertFalse(result["ok"])

    def test_check_message_with_only_integer_zero_codes_stays_ok(self):
        raw = response(body=send_body(check_message=json.dumps({"status_code": 0, "check_code": 0})))

        self.assertTrue(im_receipt.parse_send_response(raw)["ok"])

    def test_string_codes_in_check_message_are_not_accepted(self):
        for payload in ({"status_code": 0, "check_code": "0"}, {"status_code": "0"}, {"detail": {"raw_check_code": "0"}}):
            with self.subTest(payload=payload):
                raw = response(body=send_body(check_message=json.dumps(payload)))
                self.assertFalse(im_receipt.parse_send_response(raw)["ok"])

    def test_malformed_or_foreign_bodies_are_unreadable(self):
        for name, raw in {
            "empty": b"",
            "text": "not bytes",
            "json": b'{"status_code":0}',
            # Cut inside a length-delimited field, not at a field boundary
            # (a clean cut after a trailing field is still a valid message).
            "truncated": ACCEPTED[: ACCEPTED.index(LOG_ID.encode()) + 5],
            "truncated mid varint": ACCEPTED[:-2],
            "truncated varint": b"\x08\xff\xff",
            "bad wire type": b"\x0f\x00",
            "field zero": b"\x00\x01",
            "length past end": b"\x22\x7fOK",
            "replacement-mangled": ACCEPTED.decode("utf-8", "replace").encode("utf-8"),
        }.items():
            with self.subTest(name):
                result = im_receipt.parse_send_response(raw)
                self.assertTrue(result is None or result["ok"] is False, result)


class ReceiptStrengthTests(unittest.TestCase):
    def test_binary_ok_counts_as_a_strong_receipt(self):
        receipt = {"call": "message_send", "httpStatus": 200, "ok": True, "protoOk": True}

        self.assertTrue(streak_state.receipt_is_strong(receipt))

    def test_binary_ok_without_overall_ok_is_not_strong(self):
        receipt = {"call": "message_send", "httpStatus": 200, "ok": False, "protoOk": True}

        self.assertFalse(streak_state.receipt_is_strong(receipt))

    def test_unreadable_binary_receipt_is_not_strong(self):
        receipt = {"call": "message_send", "httpStatus": 200, "ok": False, "jsonParsed": False}

        self.assertFalse(streak_state.receipt_is_strong(receipt))


if __name__ == "__main__":
    unittest.main()


class ObserverReceiptTests(unittest.IsolatedAsyncioTestCase):
    """Drive the real start_im_send_observer with a fake CDP session."""

    async def _observe(self, raw, *, base64_encoded=True, status=200):
        import asyncio
        import base64
        from unittest.mock import MagicMock
        from core import tasks

        handlers = {}

        class Session:
            async def send(self, method, params=None):
                if method == "Network.getResponseBody":
                    if base64_encoded:
                        return {"body": base64.b64encode(raw).decode(), "base64Encoded": True}
                    return {"body": raw.decode("utf-8", "replace"), "base64Encoded": False}
                return {}

            def on(self, event, handler):
                handlers[event] = handler

            async def detach(self):
                return None

        page = MagicMock()
        session = Session()

        async def new_cdp_session(_page):
            return session

        page.context.new_cdp_session = new_cdp_session
        summary = await tasks.start_im_send_observer(page, "acct", "target")
        url = "https://imapi.douyin.com/v1/message/send?aid=1"
        handlers["Network.requestWillBeSent"]({"requestId": "r1", "request": {"url": url, "method": "POST", "postData": "x"}})
        handlers["Network.responseReceived"]({"requestId": "r1", "response": {"status": status, "headers": {"x-tt-logid": LOG_ID}}})
        handlers["Network.loadingFinished"]({"requestId": "r1"})
        for _ in range(20):
            await asyncio.sleep(0)
        return (await summary(extra_wait_seconds=0))["send_receipt"]

    async def test_accepted_binary_receipt_becomes_a_strong_receipt(self):
        receipt = await self._observe(ACCEPTED)

        self.assertTrue(receipt["ok"])
        self.assertTrue(receipt["protoOk"])
        self.assertTrue(streak_state.receipt_is_strong(receipt))

    async def test_refused_binary_receipt_is_a_refusal_not_a_success(self):
        from core import tasks

        receipt = await self._observe(REFUSED_8101)

        self.assertFalse(receipt["ok"])
        self.assertFalse(receipt["protoOk"])
        self.assertEqual(8101, tasks._receipt_rejection(receipt)["value"])
        self.assertFalse(streak_state.receipt_is_strong(receipt))

    async def test_accepted_body_with_http_error_is_not_ok(self):
        receipt = await self._observe(ACCEPTED, status=500)

        self.assertFalse(receipt["ok"])
        self.assertFalse(streak_state.receipt_is_strong(receipt))

    async def test_lossy_text_body_never_becomes_strong(self):
        # If the browser ever hands back the binary body as lossy text, the
        # receipt stays unconfirmed (the page-bubble fallback still applies).
        receipt = await self._observe(ACCEPTED, base64_encoded=False)

        self.assertFalse(streak_state.receipt_is_strong(receipt))


class ReviewRegressionTests(unittest.TestCase):
    """Inputs from the independent review: each must never read as accepted."""

    def assertNotAccepted(self, raw):
        result = im_receipt.parse_send_response(raw)
        self.assertTrue(result is None or result["ok"] is False, result)

    def test_duplicate_decision_field_with_a_later_refusal_is_not_accepted(self):
        self.assertNotAccepted(response(body=send_body() + vfield(3, 9)))       # send status
        self.assertNotAccepted(response(body=send_body() + vfield(5, 1)))       # check_code
        self.assertNotAccepted(response(body=send_body() + bfield(6, '{"status_code":8101}')))
        dup_outer = response(body=send_body()) + vfield(3, 7)                     # outer status
        self.assertNotAccepted(dup_outer)
        dup_desc = response(body=send_body()) + bfield(4, "REFUSED")
        self.assertNotAccepted(dup_desc)

    def test_duplicate_decision_field_even_when_both_say_ok_is_not_accepted(self):
        # Douyin never repeats these; a repeat means we do not understand the body.
        self.assertNotAccepted(response(body=send_body() + vfield(3, 0)))

    def test_non_json_or_non_object_check_message_is_not_accepted(self):
        for text in ("refused by policy", "[]", '"x"', "{bad json", "null"):
            with self.subTest(text=text):
                self.assertNotAccepted(response(body=send_body(check_message=text)))

    def test_error_desc_must_be_exactly_ok(self):
        for desc in ("REFUSED", "", "ok", "OK ", "\xff"):
            with self.subTest(desc=desc):
                self.assertNotAccepted(response(error_desc=desc, body=send_body()))
        no_desc = vfield(1, 100) + vfield(3, 0) + bfield(6, bfield(100, send_body())) + bfield(7, LOG_ID)
        self.assertNotAccepted(no_desc)
        bad_utf8 = vfield(1, 100) + vfield(3, 0) + key(4, 2) + b"\x02\xffK" + bfield(6, bfield(100, send_body()))
        self.assertNotAccepted(bad_utf8)

    def test_varint_overflow_and_non_minimal_encoding_are_not_accepted(self):
        self.assertNotAccepted(ACCEPTED + b"\x50" + b"\x80" * 9 + b"\x02")      # 2**64
        self.assertNotAccepted(ACCEPTED + b"\x50" + b"\x80" * 10 + b"\x01")     # 11 bytes
        non_minimal_zero = vfield(1, 100) + key(3, 0) + b"\x80\x00" + bfield(4, "OK") + bfield(6, bfield(100, send_body()))
        self.assertNotAccepted(non_minimal_zero)

    def test_live_shapes_still_parse_after_hardening(self):
        self.assertTrue(im_receipt.parse_send_response(ACCEPTED)["ok"])
        refused = im_receipt.parse_send_response(REFUSED_8101)
        self.assertFalse(refused["ok"])
        self.assertEqual(8101, refused["checkCodes"]["status_code"])


class SecondReviewRegressionTests(unittest.TestCase):
    def assertNotAccepted(self, raw):
        result = im_receipt.parse_send_response(raw)
        self.assertTrue(result is None or result["ok"] is False, result)

    def test_codes_nested_or_non_integer_in_check_message_are_not_accepted(self):
        for text in (
            '{"detail":{"status_code":8101}}',
            '{"detail":[{"raw_check_code":1}]}',
            '{"a":{"b":{"c":[{"check_code":"3"}]}}}',
            '{"status_code":8101.0}',
            '{"status_code":0.0}',
            '{"status_code":true}',
            '{"status_code":false}',
            '{"status_code":null}',
            '{"status_code":"0x1"}',
            '{"status_code":[0]}',
            '{"status_code":{"v":0}}',
        ):
            with self.subTest(text=text):
                self.assertNotAccepted(response(body=send_body(check_message=text)))

    def test_deeply_nested_check_message_is_not_accepted(self):
        deep = '{"a":' * 50 + "0" + "}" * 50
        self.assertNotAccepted(response(body=send_body(check_message=deep)))

    def test_zero_codes_in_nested_check_message_stay_accepted(self):
        text = json.dumps({"status_code": 0, "detail": {"raw_check_code": 0, "list": [{"check_code": 0}]}})
        self.assertTrue(im_receipt.parse_send_response(response(body=send_body(check_message=text)))["ok"])

    def test_unknown_field_in_send_body_is_not_accepted(self):
        self.assertNotAccepted(response(body=send_body() + bfield(99, '{"status_code":8101}')))
        self.assertNotAccepted(response(body=send_body() + vfield(2, 0)))
        self.assertNotAccepted(response(body=send_body() + vfield(7, 1)))

    def test_refusal_codes_are_still_reported_from_nested_json(self):
        text = '{"detail":{"status_code":8101}}'
        result = im_receipt.parse_send_response(response(body=send_body(check_message=text)))
        self.assertEqual({"status_code": 8101}, result["checkCodes"])


class ThirdReviewRegressionTests(unittest.TestCase):
    def assertNotAccepted(self, raw):
        result = im_receipt.parse_send_response(raw)
        self.assertTrue(result is None or result["ok"] is False, result)

    def test_duplicate_json_keys_in_check_message_are_not_accepted(self):
        for text in (
            '{"\\u0073tatus_code":8101,"status_code":0}',   # escaped first key
            '{"status_code":8101,"status_code":0}',
            '{"detail":{"check_code":1,"check_code":0}}',
            '{"a":1,"a":2}',                                 # any repeated key
        ):
            with self.subTest(text=text):
                self.assertNotAccepted(response(body=send_body(check_message=text)))

    def test_unknown_sibling_of_the_send_body_is_not_accepted(self):
        raw = (
            vfield(1, 100) + vfield(2, 10066) + vfield(3, 0) + bfield(4, "OK") + vfield(5, 0)
            + bfield(6, bfield(100, send_body()) + vfield(101, 8101))
            + bfield(7, LOG_ID)
        )
        self.assertNotAccepted(raw)

    def test_oversized_check_message_is_not_accepted(self):
        big = json.dumps({"status_code": 0, "pad": "x" * (im_receipt.MAX_CHECK_MESSAGE_BYTES + 1)})
        self.assertNotAccepted(response(body=send_body(check_message=big)))

    def test_live_shapes_still_parse_after_third_hardening(self):
        self.assertTrue(im_receipt.parse_send_response(ACCEPTED)["ok"])
        self.assertFalse(im_receipt.parse_send_response(REFUSED_8101)["ok"])


class ObserverDuplicateKeyTests(ObserverReceiptTests):
    async def test_escaped_duplicate_key_refusal_is_never_strong(self):
        raw = response(body=send_body(check_message='{"\\u0073tatus_code":8101,"status_code":0}'))
        receipt = await self._observe(raw)
        self.assertFalse(receipt.get("protoOk"))
        self.assertFalse(receipt["ok"])
        self.assertFalse(streak_state.receipt_is_strong(receipt))


class ObserverStringCodeTests(ObserverReceiptTests):
    async def test_string_zero_code_is_never_strong(self):
        raw = response(body=send_body(check_message='{"status_code":"0"}'))
        receipt = await self._observe(raw)
        self.assertFalse(receipt.get("protoOk"))
        self.assertFalse(streak_state.receipt_is_strong(receipt))
