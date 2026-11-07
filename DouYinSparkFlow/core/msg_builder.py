"""
core/msg_builder.py

Resolve configured message templates into concrete per-target messages.
"""

import random
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from typing import Dict, List, Optional

from utils.config import get_config
from utils.hitokoto import request_hitokoto


_MESSAGE_CONFIG = ContextVar("message_config", default=None)


def current_message_config():
    return _MESSAGE_CONFIG.get()


@contextmanager
def using_message_config(config):
    token = _MESSAGE_CONFIG.set(config)
    try:
        yield
    finally:
        _MESSAGE_CONFIG.reset(token)


FESTIVAL_WINDOW_START = date(2026, 2, 16)
FESTIVAL_WINDOW_END = date(2026, 3, 3)


def _is_holiday_mode_enabled(active_config: dict, today: date) -> bool:
    return bool(active_config.get("happyNewYear", {}).get("enabled", False)) and FESTIVAL_WINDOW_START <= today <= FESTIVAL_WINDOW_END


def _render_holiday_message(active_config: dict, today: date) -> str:
    from utils.chinese_new_year_2026_mare import get_lunar_date, get_random_festival_quote

    message = str(active_config.get("happyNewYear", {}).get("messageTemplate", "[API]"))
    if "[data]" in message:
        message = message.replace("[data]", today.strftime("%Y年%m月%d日"))
    if "[data_lunar]" in message:
        lunar_date = get_lunar_date(today)
        message = message.replace("[data_lunar]", lunar_date if lunar_date else "未知农历日期")
    if "[API]" in message:
        message = message.replace("[API]", get_random_festival_quote())
    return message.strip()


def _get_message_templates(active_config: dict) -> List[str]:
    strategy = active_config.get("sendStrategy", {}) or {}
    variants = [str(item).strip() for item in strategy.get("messageVariants", []) if str(item).strip()]
    if variants:
        return variants
    return [str(active_config.get("messageTemplate", "续火花")).strip()]


def _render_regular_message(template: str) -> str:
    message = template
    if "[API]" in message:
        message = message.replace("[API]", request_hitokoto())
    return message.strip()


def build_message_candidates(config: Optional[dict] = None) -> List[str]:
    active_config = config or current_message_config() or get_config()
    today = date.today()

    if _is_holiday_mode_enabled(active_config, today):
        return [_render_holiday_message(active_config, today)]

    candidates: List[str] = []
    for template in _get_message_templates(active_config):
        message = template.strip() if active_config.get("literalMessageTemplates") else _render_regular_message(template)
        if message and message not in candidates:
            candidates.append(message)

    if candidates:
        return candidates
    return ["续火花"]


def _extract_previous_message(previous_messages: Optional[dict], target: str) -> str:
    if not previous_messages:
        return ""

    previous = previous_messages.get(target, "")
    if isinstance(previous, dict):
        return str(previous.get("message", "")).strip()
    return str(previous).strip()


def _choose_message(candidates: List[str], previous_message: str, last_message: str) -> str:
    filtered = [message for message in candidates if message != previous_message and message != last_message]
    if filtered:
        return random.choice(filtered)

    filtered = [message for message in candidates if message != previous_message]
    if filtered:
        return random.choice(filtered)

    filtered = [message for message in candidates if message != last_message]
    if filtered:
        return random.choice(filtered)

    return random.choice(candidates)


def build_message(previous_message: str = "", config: Optional[dict] = None, last_message: str = "") -> str:
    candidates = build_message_candidates(config)
    return _choose_message(candidates, previous_message.strip(), last_message.strip()).strip()


def build_messages_for_targets(
    targets: List[str],
    previous_messages: Optional[dict] = None,
    config: Optional[dict] = None,
) -> Dict[str, str]:
    active_config = config or current_message_config() or get_config()
    strategy = active_config.get("sendStrategy", {}) or {}

    ordered_targets = []
    seen_targets = set()
    for target in targets:
        normalized = str(target).strip()
        if not normalized or normalized in seen_targets:
            continue
        seen_targets.add(normalized)
        ordered_targets.append(normalized)

    if strategy.get("shuffleTargets", True):
        random.shuffle(ordered_targets)

    planned_messages: Dict[str, str] = {}
    last_message = ""
    for target in ordered_targets:
        previous_message = _extract_previous_message(previous_messages, target)
        message = build_message(previous_message=previous_message, config=active_config, last_message=last_message)
        planned_messages[target] = message
        last_message = message

    return planned_messages
