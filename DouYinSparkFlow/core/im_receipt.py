"""Read Douyin's binary ``message_send`` receipt.

``imapi.douyin.com/v1/message/send`` answers with a protobuf ``Response``, not
JSON. Only the fields that say whether the server accepted the message are
read here; everything else is skipped. Field numbers come from the live
receipt (see tests/test_im_receipt.py):

Response
    1 cmd (varint, 100 for send)       3 status_code (varint, 0 = OK)
    2 sequence_id (varint)              4 error_desc (string, "OK")
    6 body (ResponseBody)               7 log_id (string)
ResponseBody
    100 send_message_body (SendMessageResponseBody)
SendMessageResponseBody
    1 server_message_id (varint)        4 client_message_id (string)
    3 status (varint, 0 = Succeeded)    5 check_code (varint, 0 = passed)
    6 check_message (string, JSON with its own codes on refusal)

The parser is strict and fails closed. Malformed bytes (truncation, varint
overflow or non-minimal encoding, unknown wire types) return ``None``. A
receipt is accepted only when, with no decision field repeated:
cmd is 100, status_code is 0, error_desc is exactly "OK", send status is 0,
check_code is 0 or absent, check_message is absent/empty or a JSON object
(no repeated keys, at most 8 KB) whose codes are all integer zero at every
depth, the ResponseBody holds only the send body, the send body holds only
known fields, and the server assigned a non-zero message id. Anything
else is "not accepted", which keeps today's unverified handling.
"""

from __future__ import annotations

import json
import re

SEND_CMD = 100
_SEND_BODY_FIELD = 100
_MAX_JSON_DEPTH = 8
# Live check_message bodies are a few hundred bytes; anything far larger is not
# a shape we know and is not parsed.
MAX_CHECK_MESSAGE_BYTES = 8192
# The live ResponseBody carries only the send body.
_BODY_KNOWN_FIELDS = frozenset({_SEND_BODY_FIELD})
# Fields the live send body carries: server_message_id, status,
# client_message_id, check_code, check_message. Anything else is a schema we
# have not seen and the receipt is not accepted.
_SEND_KNOWN_FIELDS = frozenset({1, 3, 4, 5, 6})
_CHECK_CODE_KEYS = ("status_code", "check_code", "raw_check_code", "err_no", "error_code")


class _Malformed(ValueError):
    pass


def _read_varint(data, pos):
    """Canonical protobuf varint: at most 10 bytes, fits in 64 bits, minimal."""
    value = 0
    start = pos
    for index in range(10):
        if pos >= len(data):
            raise _Malformed("truncated varint")
        byte = data[pos]
        pos += 1
        if index == 9 and byte > 0x01:
            raise _Malformed("varint overflows 64 bits")
        value |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            if byte == 0 and pos - start > 1:
                raise _Malformed("non-minimal varint")
            return value, pos
    raise _Malformed("varint longer than 10 bytes")


def _fields(data):
    """Yield (field_number, wire_type, value) for one message; raise if malformed."""
    pos = 0
    end = len(data)
    while pos < end:
        key, pos = _read_varint(data, pos)
        field, wire = key >> 3, key & 0x07
        if field == 0:
            raise _Malformed("field 0")
        if wire == 0:
            value, pos = _read_varint(data, pos)
        elif wire == 1:
            if pos + 8 > end:
                raise _Malformed("truncated fixed64")
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > end:
                raise _Malformed("truncated bytes")
            value, pos = data[pos:pos + length], pos + length
        elif wire == 5:
            if pos + 4 > end:
                raise _Malformed("truncated fixed32")
            value, pos = data[pos:pos + 4], pos + 4
        else:
            raise _Malformed(f"wire type {wire}")
        yield field, wire, value


def _message(data, decision_fields=()):
    """Field map; a repeated decision field makes the whole body unreadable."""
    out = {}
    for field, wire, value in _fields(data):
        if field in out:
            if field in decision_fields:
                raise _Malformed(f"decision field {field} repeated")
            continue  # repeated non-decision field: keep the first, ignore
        out[field] = (wire, value)
    return out


_OUTER_DECISION_FIELDS = (1, 3, 4, 6)
_BODY_DECISION_FIELDS = (_SEND_BODY_FIELD,)
_SEND_DECISION_FIELDS = (1, 3, 5, 6)


def _varint_field(msg, number):
    item = msg.get(number)
    if item is None:
        return None
    wire, value = item
    if wire != 0:
        raise _Malformed(f"field {number} is not a varint")
    return value


def _bytes_field(msg, number):
    item = msg.get(number)
    if item is None:
        return None
    wire, value = item
    if wire != 2:
        raise _Malformed(f"field {number} is not length-delimited")
    return bytes(value)


def _text(value):
    return value.decode("utf-8", errors="replace") if value is not None else ""


def _strict_text(value):
    """UTF-8 text or None when absent; undecodable bytes are malformed."""
    if value is None:
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Malformed("field is not UTF-8") from exc


def _raw_text_codes(text):
    codes = {}
    for key in _CHECK_CODE_KEYS:
        match = re.search(rf'"{key}"\s*:\s*(-?\d+)', text)
        if match and int(match.group(1)):
            codes[key] = int(match.group(1))
    return codes


def _integral_code(value):
    """The code for a JSON integer; None (not understood) for anything else.

    Strings are refused too, even "0": the live refusal uses JSON integers, and
    a code in another type is a shape we have not seen.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class _DuplicateKey(ValueError):
    pass


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(key)
        out[key] = value
    return out


def _check_message_codes(text):
    """(understood, non-zero codes) for the check_message text.

    Empty means nothing to say. A JSON object is walked at every depth (objects
    and arrays). It is understood only when every known code key, wherever it
    appears, holds a JSON integer; strings, floats, booleans, null, nested
    containers or a nesting deeper than _MAX_JSON_DEPTH are not understood.
    The caller accepts only "understood and no non-zero code". Codes visible in
    unparseable text are still reported for the refusal record.
    """
    if not text:
        return True, {}
    if len(text.encode("utf-8", errors="replace")) > MAX_CHECK_MESSAGE_BYTES:
        return False, _raw_text_codes(text[:MAX_CHECK_MESSAGE_BYTES])
    try:
        # Repeated keys (after JSON unescaping) would let a later value hide an
        # earlier refusal code, so the whole message is "not understood".
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        return False, _raw_text_codes(text)
    codes = {}
    understood = True
    stack = [(data, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > _MAX_JSON_DEPTH:
            understood = False
            continue
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _CHECK_CODE_KEYS:
                    code = _integral_code(value)
                    if code is None:
                        understood = False
                    elif code:
                        codes.setdefault(key, code)
                elif isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node if isinstance(item, (dict, list)))
    if not understood and not codes:
        codes = _raw_text_codes(text)
    return understood, codes


def parse_send_response(raw):
    """Summary of a binary send receipt, or ``None`` when it is not one.

    Returns ``{"ok", "cmd", "statusCode", "errorDesc", "sendStatus",
    "checkCode", "hasServerMessageId", "checkCodes", "logId"}``. ``ok`` is
    True only for an unambiguous acceptance.
    """
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return None
    data = bytes(raw)
    if data[:1] in (b"{", b"["):
        return None  # JSON body: the JSON reader handles it
    try:
        outer = _message(data, _OUTER_DECISION_FIELDS)
        cmd = _varint_field(outer, 1)
        status_code = _varint_field(outer, 3)
        error_desc = _strict_text(_bytes_field(outer, 4))
        log_id = _text(_bytes_field(outer, 7))
        body_bytes = _bytes_field(outer, 6)
        send = None
        unknown_body_fields = False
        if body_bytes is not None:
            body = _message(body_bytes, _BODY_DECISION_FIELDS)
            unknown_body_fields = bool(set(body) - _BODY_KNOWN_FIELDS)
            send_bytes = _bytes_field(body, _SEND_BODY_FIELD)
            if send_bytes is not None:
                send = _message(send_bytes, _SEND_DECISION_FIELDS)
        unknown_send_fields = bool(send is not None and set(send) - _SEND_KNOWN_FIELDS)
        send_status = check_code = None
        has_message_id = False
        check_understood, check_codes = True, {}
        if send is not None:
            server_message_id = _varint_field(send, 1)
            has_message_id = bool(server_message_id)
            send_status = _varint_field(send, 3)
            check_code = _varint_field(send, 5)
            check_understood, check_codes = _check_message_codes(_text(_bytes_field(send, 6)))
    except _Malformed:
        return None
    if cmd is None and status_code is None and send is None:
        return None  # nothing we recognise: not this message type
    ok = bool(
        cmd == SEND_CMD
        and status_code == 0
        and error_desc == "OK"
        and send is not None
        and send_status == 0
        and (check_code in (None, 0))
        and check_understood
        and not check_codes
        and not unknown_send_fields
        and not unknown_body_fields
        and has_message_id
    )
    return {
        "ok": ok,
        "cmd": cmd,
        "statusCode": status_code,
        "errorDesc": (error_desc or "")[:80],
        "sendStatus": send_status,
        "checkCode": check_code,
        "hasServerMessageId": has_message_id,
        "checkCodes": check_codes,
        "logId": log_id[:120],
    }
