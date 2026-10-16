"""Local settings, device exclusion and submission tracking for owned Android tasks."""
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

import store
from .protocol_common import CURRENT, ActionRequired, ProtocolError


class NativeSettings:
    ROOT_DIR = Path(__file__).resolve().parents[3]

    @property
    def VIVO_NATIVE_DEVICES(self):
        raw = os.environ.get('VIVO_NATIVE_DEVICES')
        path = store.DATA / 'native-devices.json'
        try:
            devices = json.loads(raw if raw is not None else path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as exc:
            raise ProtocolError('原生设备配置无法读取，请检查 native-devices.json。') from exc
        if not isinstance(devices, dict) or any(not isinstance(v, dict) for v in devices.values()):
            raise ProtocolError('原生设备配置必须按本项目账号 ID 映射设备。')
        return devices


settings = NativeSettings()


def mark_submission():
    context = CURRENT.get()
    if context is None:
        raise ProtocolError('原生操作缺少账号执行上下文。')
    context.ensure_allowed()
    context.transport.unsafe_sent = True


@contextmanager
def device_lock(adb, serial):
    # A physical serial is shared across all accounts and worker processes,
    # even if they refer to different adb installations.
    folder = store.DATA / 'device-locks'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(serial.encode()).hexdigest() + '.lock')
    stream = path.open('a+b')
    try:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ActionRequired('本人原生设备正被其他任务占用，稍后再核验。', reason_kind='native_context') from exc
        yield
    finally:
        stream.close()
