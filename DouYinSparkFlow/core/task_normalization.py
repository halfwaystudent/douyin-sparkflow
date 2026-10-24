import unicodedata


def _safe_name(value):
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)[:80]


def _normalize_target_name(value):
    raw = unicodedata.normalize("NFKC", str(value or ""))
    for token in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        raw = raw.replace(token, "")
    raw = raw.replace("\xa0", " ")
    return " ".join(raw.split()).strip().casefold()


def _coerce_non_negative_int(value, default):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _coerce_positive_int(value, default):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def _coerce_positive_float(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if parsed <= 0:
        parsed = float(default)
    return parsed


def _normalize_friend_list_scan_config(active_config):
    raw = active_config.get("friendListScan", {}) or {}
    return {
        "maxScanSeconds": _coerce_positive_int(raw.get("maxScanSeconds", 300), 300),
        "idleScanSeconds": _coerce_positive_int(raw.get("idleScanSeconds", 120), 120),
        "scrollStepPx": _coerce_positive_int(raw.get("scrollStepPx", 400), 400),
        "scrollDelaySeconds": _coerce_positive_float(raw.get("scrollDelaySeconds", 1.5), 1.5),
    }


def _normalize_send_strategy(active_config):
    raw = active_config.get("sendStrategy", {}) or {}
    start_min = _coerce_non_negative_int(raw.get("accountStartDelaySecondsMin", 0), 0)
    start_max = _coerce_non_negative_int(raw.get("accountStartDelaySecondsMax", start_min), start_min)
    if start_max < start_min:
        start_max = start_min

    message_min = _coerce_non_negative_int(raw.get("messageIntervalSecondsMin", 0), 0)
    message_max = _coerce_non_negative_int(raw.get("messageIntervalSecondsMax", message_min), message_min)
    if message_max < message_min:
        message_max = message_min

    return {
        "shuffleTargets": bool(raw.get("shuffleTargets", True)),
        "accountStartDelaySecondsMin": start_min,
        "accountStartDelaySecondsMax": start_max,
        "messageIntervalSecondsMin": message_min,
        "messageIntervalSecondsMax": message_max,
    }
