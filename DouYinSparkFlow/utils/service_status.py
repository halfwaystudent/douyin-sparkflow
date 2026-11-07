"""Read a small host-owned status snapshot; no container-engine privileges."""
from datetime import datetime, timezone
import json
from pathlib import Path

SERVICE_NAMES = frozenset({'douyin-web', 'douyin-scheduler', 'login-desktop', 'mihomo'})
SERVICE_STATES = frozenset({'running', 'exited', 'created', 'restarting', 'dead', 'removing', 'paused', 'not_deployed', 'unavailable'})
MAX_SNAPSHOT_BYTES = 32768
MAX_SNAPSHOT_AGE = 60


def read_host_status_snapshot(path):
    failure = {'ok': False, 'containers': [], 'error': '暂时无法读取服务状态快照，请稍后重新检查。'}
    try:
        source = Path(path)
        if not source.is_file():
            return failure
        with source.open('rb') as handle:
            data = handle.read(MAX_SNAPSHOT_BYTES + 1)
        if len(data) > MAX_SNAPSHOT_BYTES:
            return failure
        snapshot = json.loads(data)
        if not isinstance(snapshot, dict) or snapshot.get('version') != 1:
            return failure
        checked = datetime.fromisoformat(snapshot['checkedAt'])
        if checked.utcoffset() is None:
            return failure
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        if age < -5:
            return failure
        if age > MAX_SNAPSHOT_AGE:
            return {**failure, 'error': '服务状态快照已过期，暂时无法确认服务是否运行，请稍后重新检查。'}
        source_rows = snapshot['containers']
        if not isinstance(source_rows, list) or len(source_rows) != len(SERVICE_NAMES):
            return failure
        rows = []
        seen = set()
        for row in source_rows:
            if not isinstance(row, dict):
                return failure
            name, state, status = row.get('Names'), row.get('State'), row.get('Status')
            if not isinstance(name, str) or name not in SERVICE_NAMES or name in seen:
                return failure
            if not isinstance(state, str) or state not in SERVICE_STATES:
                return failure
            if not isinstance(status, str) or not status.strip() or len(status) > 160:
                return failure
            seen.add(name)
            rows.append({'Names': name, 'State': state, 'Status': status})
        return {'ok': True, 'containers': rows, 'checkedAt': checked.isoformat(timespec='seconds')}
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return failure
