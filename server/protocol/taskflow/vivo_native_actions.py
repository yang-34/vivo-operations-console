# Bundled vivo protocol reference; see source-manifest.json.
"""Actual owned Game Center UI actions; completion is read from official EC."""
import importlib
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse
from .native_runtime import settings
from .native_runtime import device_lock
from .native_runtime import mark_submission
from .protocol_common import ActionRequired, ProtocolError
from .sdk import PlatformError
PACKAGE = 'com.vivo.game'

class NativeActions:
    package = PACKAGE
    identity_script = 'vivo-native-identity.js'
    app_name = '游戏中心'

    def __init__(self, context):
        self.context = context
        self.config = settings.VIVO_NATIVE_DEVICES.get(str(context.account_id), {})
        self.script = self.session = self.lock = None
        self.before_input = None
        self.input_started = False

    def begin_action(self):
        if self.before_input and (not self.input_started):
            self.before_input()
            self.input_started = True
        mark_submission()

    def adb(self, *args):
        self.context.ensure_allowed()
        if args[:2] == ('shell', 'input') or args[:5] == ('shell', 'am', 'start', '-a', 'android.intent.action.VIEW'):
            self.begin_action()
        result = subprocess.run([self.config['adb_path'], '-s', self.config['serial'], *args], capture_output=True, timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ActionRequired('本人原生设备未完成当前操作，本项未计为完成。', reason_kind='native_context')
        return result.stdout.decode('utf-8', 'replace')

    def setup(self):
        try:
            self._setup()
        except PlatformError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise ActionRequired(f'本人{self.app_name}原生环境暂不可用，本项跳过验收，其他免费任务继续。', reason_kind='session_unavailable') from exc

    def _setup(self):
        if self.script:
            return
        if not all((self.config.get(k) for k in ('adb_path', 'serial', 'frida_path'))):
            raise ActionRequired(f'本账号没有可用{self.app_name}原生执行设备，本次跳过该原生账号验收。', reason_kind='session_unavailable')
        if not re.fullmatch('[A-Za-z0-9._:-]{1,128}', self.config['serial']):
            raise ProtocolError('原生设备标识格式不正确。')
        self.lock = device_lock(self.config['adb_path'], self.config['serial'])
        self.lock.__enter__()
        if not self.adb('shell', 'pm', 'path', self.package).strip():
            raise ActionRequired(f'已绑定设备未安装{self.app_name}，本次跳过原生账号验收。', reason_kind='session_unavailable')
        launch = self.adb('shell', 'am', 'start', '-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.LAUNCHER', '-p', self.package)
        if 'Error:' in launch:
            raise ActionRequired(f'已绑定设备无法启动{self.app_name}，跳过原生账号验收。', reason_kind='session_unavailable')
        module = str(Path(self.config['frida_path']).resolve())
        if module not in sys.path:
            sys.path.insert(0, module)
        frida = importlib.import_module('frida')
        port = int(self.config.get('frida_port', 27184))
        self.adb('forward', f'tcp:{port}', 'tcp:27042')
        remote = frida.get_device_manager().add_remote_device(f'127.0.0.1:{port}')
        target = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.context.ensure_allowed()
            target = next((a for a in remote.enumerate_applications() if a.identifier == self.package and a.pid), None)
            if target:
                break
            time.sleep(1)
        if target is None:
            raise ActionRequired(f'{self.app_name}没有可运行的原生会话，本次跳过。', reason_kind='session_unavailable')
        self.session = remote.attach(target.pid)
        self.script = self.session.create_script((settings.ROOT_DIR / 'scripts' / self.identity_script).read_text(encoding='utf-8'))
        self.script.load()
        self.verify_owner()

    def verify_owner(self):
        owned = self.script.exports_sync.identity().get('openid')
        if not owned or str(owned) != str(self.context.credentials['session']['openid']):
            raise ActionRequired(f'手机{self.app_name}账号与当前账号不一致，本次跳过原生验收。', reason_kind='session_unavailable')

    def ui(self):
        self.adb('shell', 'uiautomator', 'dump', '/sdcard/taskflow-vivo-ui.xml')
        raw = self.adb('shell', 'cat', '/sdcard/taskflow-vivo-ui.xml')
        return ET.fromstring(raw[raw.find('<?xml'):])

    def click(self, labels, allowed=None):
        allowed = allowed or (self.package,)
        self.verify_owner()
        root = self.ui()
        texts = [n.get('text', '') for n in root.iter('node')]
        if any((t in ('确认支付', '立即支付', '请输入支付密码', '立即借款', '提交贷款申请') for t in texts)):
            raise ActionRequired('当前页面进入付费或借款流程，本项停止，不消费积分或资金。', reason_kind='manual')
        matches = [n for n in root.iter('node') if n.get('package') in allowed and n.get('text') in labels and (n.get('enabled') == 'true')]
        if len(matches) != 1:
            return False
        bounds = list(map(int, re.findall('\\d+', matches[0].get('bounds', ''))))
        if len(bounds) != 4:
            raise ProtocolError('原生控件位置无法确认。')
        self.adb('shell', 'input', 'tap', str((bounds[0] + bounds[2]) // 2), str((bounds[1] + bounds[3]) // 2))
        time.sleep(1)
        return True

    def operate(self, task, label):
        self.setup()
        if label in ('加入聊天频道', '佩戴频道身份'):
            self.click(('频道', '游戏频道', '群聊'))
            if label == '加入聊天频道':
                submitted = self.click(('加入频道', '加入聊天频道', '立即加入'))
            else:
                self.click(('我的身份', '频道身份'))
                submitted = self.click(('免费佩戴', '佩戴身份', '立即佩戴', '佩戴'))
            if not submitted:
                raise ActionRequired('原生当前页面没有唯一可执行的频道控件，未伪报加入或佩戴；其他任务继续。', reason_kind='native_context')
            return {'native_action': label, 'physical_ui_submitted': True}
        if label == '安装并体验应用':
            uri = task.get('jumpURL') or task.get('jumpUrl') or task.get('url')
            package = task.get('appPkg') or task.get('packageName')
            if not uri or not package or (not re.fullmatch('[A-Za-z0-9_]+(?:\\.[A-Za-z0-9_]+)+', package)):
                raise ActionRequired('本任务需要服务器下发的真实应用包名和商店入口，未猜测安装对象。', reason_kind='missing_context')
            parsed = urlparse(uri)
            if parsed.scheme not in ('market', 'vivomarket'):
                raise ProtocolError('应用体验入口不是已核验的官方商店路由。')
            if any((str(task.get(k, 0)) not in ('0', '0.0', 'None', '') for k in ('price', 'cost', 'pointsCost'))):
                raise ActionRequired('此应用有费用，未安装或消费。', reason_kind='manual')
            self.adb('shell', 'am', 'start', '-a', 'android.intent.action.VIEW', '-d', uri, '-p', 'com.vivo.appstore')
            if not self.click(('安装', '下载'), allowed=('com.vivo.appstore',)):
                raise ActionRequired('官方商店没有唯一免费安装控件，未提交下载。', reason_kind='native_context')
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                self.context.ensure_allowed()
                if self.adb('shell', 'pm', 'path', package).strip():
                    self.adb('shell', 'am', 'start', '-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.LAUNCHER', '-p', package)
                    time.sleep(5)
                    return {'native_action': label, 'package_installed': True, 'application_launched': True}
                time.sleep(3)
            raise ActionRequired('真实应用尚未安装完成，未记录为体验完成。', reason_kind='native_context')
        raise ProtocolError('未注册的vivo原生操作。')

    def close(self):
        if self.session:
            try:
                self.session.detach()
            except Exception:
                pass
        if self.lock:
            self.lock.__exit__(None, None, None)
        self.session = self.script = self.lock = None

def perform(context, task, label):
    driver = NativeActions(context)
    try:
        return driver.operate(task, label)
    except (ImportError, OSError, ValueError) as error:
        raise ActionRequired('该账号原生执行服务不可用，本次跳过原生验收。', reason_kind='session_unavailable') from error
    finally:
        driver.close()

class MusicActions(NativeActions):
    package = 'com.android.bbkmusic'
    identity_script = 'vivo-account-identity.js'
    app_name = '音乐'

    def operate(self, task, label):
        self.setup()
        uri = str(task.get('jumpURL') or task.get('jumpUrl') or '')
        parsed = urlparse(uri)
        if not uri or not (parsed.scheme in ('vivomusic', 'bbkmusic', 'music') or (parsed.scheme == 'https' and (parsed.hostname or '').endswith('.vivo.com.cn'))):
            raise ActionRequired('当前听书任务没有官方下发的专辑入口，未猜测收藏对象。', reason_kind='missing_context')
        result = self.adb('shell', 'am', 'start', '-a', 'android.intent.action.VIEW', '-d', shlex.quote(uri), '-p', self.package)
        if 'Error:' in result:
            raise ActionRequired('本人音乐客户端无法打开当期听书入口。', reason_kind='native_context')
        if not self.click(('收藏专辑',)):
            raise ActionRequired('当前专辑没有唯一收藏控件，未用SDK计数冒充收藏。', reason_kind='native_context')
        if not any((n.get('package') == self.package and n.get('text') in ('已收藏', '取消收藏') for n in self.ui().iter('node'))):
            raise ActionRequired('专辑收藏状态尚未回读，未记任务完成。', reason_kind='settlement')
        return {'physical_album_favorite': True, 'favorite_state_read_back': True}
