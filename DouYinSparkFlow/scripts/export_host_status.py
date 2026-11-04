#!/usr/bin/env python3
"""Export only four logical service states; run on the host, not inside Web UI."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROLES = ('douyin-web', 'douyin-scheduler', 'login-desktop', 'mihomo')
LABELS = {'running': '运行中', 'exited': '已停止', 'created': '已创建，尚未启动',
          'restarting': '正在重启', 'dead': '异常停止', 'removing': '正在移除', 'paused': '已暂停'}


def inspect_state(name):
    try:
        result = subprocess.run(['docker', 'container', 'inspect', '--format', '{{.State.Status}}', name],
                                capture_output=True, text=True, check=False, timeout=3,
                                env={**os.environ, 'LANG': 'C', 'LC_ALL': 'C'})
        if result.returncode == 0:
            state = result.stdout.strip()
            if state in LABELS:
                return state
        elif 'No such container:' in result.stderr or 'No such object:' in result.stderr:
            return 'not_deployed'
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 'unavailable'


def export_snapshot(output, mappings):
    rows = []
    for role in ROLES:
        state = inspect_state(mappings[role]) if role in mappings else 'not_deployed'
        status = LABELS.get(state, '未部署' if state == 'not_deployed' else '暂时无法读取')
        if role == 'douyin-scheduler' and state == 'not_deployed':
            status = '未部署（未启用定时发送）'
        rows.append({'Names': role, 'State': state, 'Status': status})
    snapshot = {'version': 1, 'checkedAt': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'containers': rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent,
                                         prefix='.status.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(snapshot, handle, ensure_ascii=False)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--service', action='append', default=[], metavar='ROLE=CONTAINER')
    args = parser.parse_args()
    mappings = {}
    for item in args.service:
        role, separator, name = item.partition('=')
        if not separator or role not in ROLES or role in mappings or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name):
            parser.error('service must map a unique known role to a valid container name')
        mappings[role] = name
    if not args.service:
        mappings = {role: role for role in ROLES}
    export_snapshot(args.output, mappings)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
