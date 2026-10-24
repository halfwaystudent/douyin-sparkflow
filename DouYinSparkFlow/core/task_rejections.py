import re


def _json_body_success(data):
    if not isinstance(data, dict):
        return None

    candidates = []
    pending = [data]
    while pending:
        candidate = pending.pop(0)
        if isinstance(candidate, dict):
            candidates.append(candidate)
            for value in candidate.values():
                if isinstance(value, (dict, list)):
                    pending.append(value)
        elif isinstance(candidate, list):
            pending.extend(
                value
                for value in candidate
                if isinstance(value, (dict, list))
            )

    code_seen = False
    code_success = True
    error_seen = False
    for candidate in candidates:
        for key in ("status_code", "err_no", "errno", "error_code", "code"):
            if key in candidate:
                code_seen = True
                if candidate.get(key) not in (0, "0"):
                    code_success = False
        message = str(
            candidate.get("message")
            or candidate.get("status_msg")
            or ""
        ).lower()
        if message and message not in ("success", "ok"):
            error_seen = True
        for key in ("error", "error_msg", "err_msg"):
            if candidate.get(key):
                error_seen = True

    if code_seen:
        return code_success and not error_seen
    if error_seen:
        return False
    return None


# Response keys whose non-zero value means the server refused this message.
# Deliberately narrow: generic metadata such as ``msg_type`` and free-form
# fields such as ``decision_type`` are not refusal evidence, and neither is the
# message text the operator asked us to send.
SEND_REJECTION_CODE_KEYS = (
    "status_code",
    "err_no",
    "errno",
    "error_code",
    "check_code",
    "raw_check_code",
)

_SEND_REJECTION_PATTERNS = tuple(
    (key, re.compile(rf'"{re.escape(key)}"\s*:\s*(-?\d+)'))
    for key in SEND_REJECTION_CODE_KEYS
)


def _coerce_rejection_code(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def _rejection_from_json(data):
    """First non-zero refusal code inside a parsed response body."""
    pending = [data]
    while pending:
        candidate = pending.pop(0)
        if isinstance(candidate, dict):
            for key in SEND_REJECTION_CODE_KEYS:
                if key not in candidate:
                    continue
                code = _coerce_rejection_code(candidate.get(key))
                if code:
                    return {"key": key, "value": code, "source": "json"}
            pending.extend(
                value
                for value in candidate.values()
                if isinstance(value, (dict, list))
            )
        elif isinstance(candidate, list):
            pending.extend(
                value for value in candidate if isinstance(value, (dict, list))
            )
    return None


def _rejection_from_raw_text(body_text):
    """First non-zero refusal code inside a body we could not parse as JSON.

    Douyin answers ``message_send`` with protobuf-wrapped JSON, so the refusal
    code is visible in the raw bytes long before any JSON parser sees it. Only
    the quoted JSON form counts: free text that happens to mention a code, such
    as the message we just tried to send, is not refusal evidence.
    """
    text = str(body_text or "")
    if not text:
        return None
    for key, pattern in _SEND_REJECTION_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        code = _coerce_rejection_code(match.group(1))
        if code:
            return {"key": key, "value": code, "source": "raw"}
    return None


def _extract_send_rejection(data, body_text):
    """Refusal evidence from a ``message_send`` response, or ``None``.

    Only a non-zero value on a known code field counts. A response we cannot
    parse and that carries no such field stays unknown: it is neither evidence
    of a refusal nor evidence of acceptance.
    """
    if isinstance(data, dict):
        # A body we could parse is authoritative: read its fields, never scan
        # its text, so a string value cannot masquerade as a refusal code.
        return _rejection_from_json(data)
    return _rejection_from_raw_text(body_text)


def _receipt_rejection(receipt):
    """Refusal recorded on a send receipt, if the observer found one."""
    if not isinstance(receipt, dict):
        return None
    rejection = receipt.get("rejection")
    return dict(rejection) if isinstance(rejection, dict) else None


def _should_persist_recovered_browser_evidence(sent_ok, error):
    """Whether the page bubble may stand in for the server's answer.

    A refusal is an explicit answer, so the page must not rewrite it as sent.
    An unreadable or unknown response keeps the existing fallback: the bubble
    is the only evidence available and the attempt is recorded as unverified.
    """
    if isinstance(error, BrowserSendRejected) or _receipt_rejection(
        getattr(error, "receipt", None)
    ):
        return False
    return bool(sent_ok)


class BrowserSendRejected(RuntimeError):
    """``message_send`` answered with an explicit refusal code.

    Carries the code so the failure record and the run report show which field
    refused the message instead of a generic "receipt rejected".
    """

    def __init__(self, message, rejection=None):
        super().__init__(message)
        self.rejection = dict(rejection or {})
