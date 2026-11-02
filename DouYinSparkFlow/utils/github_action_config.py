"""Compatibility notice for the retired GitHub credential export."""


def compress_users_data():
    raise RuntimeError("GitHub Actions 凭据导出已停用，请使用本机 scheduler。")


def print_github_action_config():
    print("GitHub 定时发送已移除，不再导出或复制账号凭据。请使用本机 scheduler。")
