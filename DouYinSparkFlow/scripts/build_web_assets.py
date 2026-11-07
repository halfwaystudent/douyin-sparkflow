"""Split the independently scoped page controllers without a JS toolchain."""

from pathlib import Path


def split_scripts(source):
    workspace_start = source.index('(() => {\n  const root = document.getElementById("login-desktop-controls");')
    picker_start = source.index('(() => {\n  const parseJson = (id, fallback = []) => {', workspace_start)
    icons_start = source.index('(() => {\n  const renderAvailableIcons = () => {', picker_start)
    return {
        "app-shell.js": source[:workspace_start] + source[icons_start:],
        "login-workspace.js": source[workspace_start:picker_start],
        "account-pickers.js": source[picker_start:icons_start],
    }


if __name__ == "__main__":
    directory = Path(__file__).resolve().parents[1] / "webui/static"
    source = (directory / "app.js").read_text(encoding="utf-8")
    for name, content in split_scripts(source).items():
        (directory / name).write_text(content, encoding="utf-8")
        print("{}: {} bytes".format(name, len(content.encode("utf-8"))))
