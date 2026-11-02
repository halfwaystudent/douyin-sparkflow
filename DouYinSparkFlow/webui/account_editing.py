"""Versions for editable account settings, independent of refresh progress."""

import hashlib
import json


def account_edit_revision(account):
    material = {
        "account_ref": str(account.get("account_ref") or ""),
        "username": str(account.get("username") or ""),
        "enabled": bool(account.get("enabled", True)),
        "targets": list(account.get("targets") or []),
        "group_target_bindings": dict(account.get("group_target_bindings") or {}),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AccountEditConflict(ValueError):
    def __init__(self, account, message="这个账号的设置已在另一页面更新。本次内容尚未保存，请先核对最新设置。"):
        self.revision = account_edit_revision(account)
        super().__init__(message)
