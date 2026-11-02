"""The public-to-owner projection used by the account candidate lists."""


def picker_cache(account):
    candidates = account.get("friends_cache") or []
    if not isinstance(candidates, list):
        candidates = []
    friends = list(dict.fromkeys(name.strip() for name in candidates if isinstance(name, str) and name.strip()))
    groups = []
    seen = set()
    for group in account.get("group_cache") or []:
        if not isinstance(group, dict):
            continue
        raw_id = group.get("conversationId")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            continue
        conversation_id = str(raw_id).strip()
        if not conversation_id or conversation_id in seen:
            continue
        seen.add(conversation_id)
        name = group.get("name")
        groups.append({"conversationId": conversation_id, "name": name.strip() if isinstance(name, str) and name.strip() else "未命名群"})
    return {"friends": friends, "groups": groups}
