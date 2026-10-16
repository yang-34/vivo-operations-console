"""Built-in protocol constants and signing primitives; no account or proxy secrets."""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import random
import re
import struct
import threading
import time
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
_LOG_TLS = threading.local()
UA_SPACE = 'VivoSpace/2.1 (Android)_(vivo.com)/13.4.1.5_13415 DeviceType/phone'
POINTH5 = 'https://pointh5.vivo.com.cn'
POINTWAP = 'https://pointwap.vivo.com.cn'
POINTSDK = 'https://pointsdk.vivo.com.cn'
SIGNHOST = 'https://sign.vivo.com.cn'
MEMBER = 'https://member.vivo.com.cn'
BBS = 'https://bbs.vivo.com.cn'
EDEN = 'https://eden.vivo.com.cn'
MSHOPACT = 'https://mshopact.vivo.com.cn'
SHOP_FBAPI = 'https://shop.vivo.com.cn/wap/fbApi'
WUKONGAPI = 'https://wukongapi.vivo.com.cn'
SCRM_ORIGIN = 'https://scrmact.vivo.com.cn'
POINTACT = 'https://pointactivity.vivo.com.cn'
MPC = 'https://mpc.vivo.com.cn'
H5_REFERER = 'https://zhan.vivo.com.cn/point/wk2207264eeaa8ec'
H5_ORIGIN = 'https://zhan.vivo.com.cn'
WX_APPID = 'wxeb4a6201a90783f4'
FORUM_SIG_KEY = 'vivospaceprivkey=j56Hi8o1xzu6Gp'
SIGN_VERSION = '2'
BIZ_PACKAGE = {'appstore': 'com.bbk.appstore', 'gamecenter': 'com.vivo.game', 'vivospace': 'com.vivo.space', 'browser': 'com.vivo.browser', 'videoPlayer': 'com.android.VideoPlayer', 'wallet': 'com.vivo.wallet', 'iTheme': 'com.bbk.theme', 'iMusic': 'com.android.bbkmusic', 'minigame': 'com.vivo.minigamecenter', 'quickapp': 'com.quickapp.center', 'vhome': 'com.vivo.vhome', 'vivop': ''}
PN_MAP = {'wallet': 'com.vivo.wallet', 'gamecenter': 'com.vivo.game', 'vivospace': 'com.vivo.space', 'appstore': 'com.bbk.appstore', 'browser': 'com.vivo.browser', 'videoPlayer': 'com.android.VideoPlayer', 'iTheme': 'com.bbk.theme', 'iMusic': 'com.android.bbkmusic', 'minigame': 'com.vivo.minigamecenter', 'quickapp': 'com.quickapp.center', 'vhome': 'com.vivo.vhome', '应用商店': 'com.bbk.appstore', '游戏中心': 'com.vivo.game', 'vivo官网': 'com.vivo.space', '浏览器': 'com.vivo.browser', 'vivo视频': 'com.android.VideoPlayer', '钱包': 'com.vivo.wallet', '主题': 'com.bbk.theme', '音乐': 'com.android.bbkmusic', '小游戏': 'com.vivo.minigamecenter', '快应用': 'com.quickapp.center', '智慧生活': 'com.vivo.vhome'}
ALL_PKGS = ['com.vivo.space'] + [p for p in BIZ_PACKAGE.values() if p]
MAIN_SIGN_PKG = 'com.vivo.wallet'
GAP = float(os.environ.get('VIVO_GAP') or '0.6')
TOKEN_REUSE_SECONDS = int(os.environ.get('VIVO_TOKEN_REUSE') or 5 * 86400)

def now_ms() -> int:
    return int(time.time() * 1000)

def now_ts() -> float:
    return time.time()
VIVO_APPID = 'wxeb4a6201a90783f4'

def _socks_ok() -> bool:
    """检测当前环境是否支持 SOCKS 代理（缺 pysocks 时 requests 报
    'Missing dependencies for SOCKS support'）。

    缺依赖时自动降级直连并在启动时提示一次装法：
      pip install pysocks   （或 pip install requests[socks]）
    """
    global _SOCKS_OK_CACHE
    if _SOCKS_OK_CACHE is None:
        try:
            import socks
            _SOCKS_OK_CACHE = True
        except ImportError:
            _SOCKS_OK_CACHE = False
            log('  ⚠️ 缺 pysocks → SOCKS 代理不可用，本次直连（装依赖后自动启用代理：pip install pysocks）')
    return _SOCKS_OK_CACHE
_SOCKS_OK_CACHE: Optional[bool] = None

def _px(proxy: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """代理参数安全化：不支持 SOCKS 或未配代理时返回 None（直连）。"""
    if not proxy:
        return None
    if not _socks_ok():
        return None
    return proxy

def _luhn(body14: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(body14)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)

def gen_imei(seed: str) -> str:
    h = hashlib.sha256(seed.encode()).hexdigest()
    tac = '86' + str(int(h[0:6], 16) % 1000000).zfill(6)
    serial = str(int(h[6:18], 16) % 1000000).zfill(6)
    body = (tac + serial)[:14]
    return body + _luhn(body)

def gen_oaid(seed: str) -> str:
    return str(uuid.UUID(hashlib.md5(('oaid:' + seed).encode()).hexdigest()))
NET_PREFIX = 'R7cF8SoztcIzrECAIiz%2F0buRl2GGnQF4QJwyuINtTLWSIjNI9TsfuvNQTxiq%2BP0yAVt1ZRSyy5AsSfeNGwoa6AmPkwxjpni6quiOuuID1wZbrrj2PvG9%2B069AZLTu%2Fyr0luWfhhKDPGtYpuk4xBZrHvsbVvgjHGwqY6zIQFFGy6lNVfTvKV%2BMqOVHM8X6Qiphp2ckuQfZd5oqXcQSpUwEC8GnzKSv2XA7QfOG4kdVwxu9WNAUas0fky1Cq%2F%2Bj%2BttR6TRzWwBFMQZEX02LTQn6YyB6LyftWv%2F0YPlU6Hh38scNSTeTaOW1j8vMxdGB6bWg1fZNIq3T7t%2FU3W%2FQyz'

def _wave_net(s: str) -> int:
    rev = hashlib.md5((NET_PREFIX + s).encode()).hexdigest()[::-1]
    return int(rev[7:14], 16) + int(rev[14:24], 16) & 4294967295

def _utf8_only(s: str) -> str:
    raw = s.encode()
    out = bytearray()
    i, n = (0, len(raw))
    while i < n:
        b = raw[i]
        if b & 248 == 240 and n - i >= 4:
            i += 4
            continue
        out.append(b)
        i += 1
    return out.decode('utf-8', 'replace')

def get_sign(values: List[str]) -> str:
    from urllib.parse import quote
    sb = ''.join((':' + str(v) for v in values))
    inner = _wave_net(_utf8_only(sb))
    return quote(f'{SIGN_VERSION}|{inner}', safe='')

def _url_values(url: str) -> List[str]:
    if not url:
        return []
    s = url.split('?', 1)[1] if '?' in url else url
    if not s or '=' not in s:
        return []
    out, seen = ([], {})
    from urllib.parse import unquote
    for part in s.split('&'):
        kv = part.split('=')
        k = kv[0]
        v = unquote(kv[1]) if len(kv) > 1 else ''
        if k in seen:
            try:
                out.remove(seen[k])
            except ValueError:
                pass
        else:
            out.append(v)
        seen[k] = v
    return out

def m_value_for_post(param_map: Dict[str, Any], extra_url: str='') -> str:
    vals = ['' if v is None else str(v) for v in param_map.values()]
    vals.extend(_url_values(extra_url))
    vals.sort()
    return get_sign(vals)

class MaodunCipher:
    MAGIC = 203
    PROTOCOL_TYPE = 32
    KEY_VERSION = 1
    ENC_TYPE = 65
    KEY_TOKEN = b'com.vivo.point.mall'
    CHARS = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    _PEM_BODY = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAhYDpLPqfVnPs2zV6IPTBt743XC8vNSiuqaQXopavStHuTgopj7iuwvPCGae7d1tB0stu6lVIFG/NBcQeOz308+AI5kcdIfA5HNri4vlT0cUCDTs02l/BEsZRGuk1MhL5ky+ka00JzVXbS4Or6QnMHrX6H/vmrDsbsyiCuO7NZj6zZA3VWov2GO7UaziuMgmrMYcT1WvRK9P3NMMaA4oZrA5+yhNCrYXFaSOmJGNOpSBS8LMI+aGOsjVyoJdBrhdrwaQuKALQ3jAqNmN+6OdJR04JIxpCNJUauCiKarWwTOjD7F6UdNSd2N6N/YMIYlA6A6xCC+ot9Y7uD3VsejKcuQIDAQAB'

    def __init__(self):
        import base64
        from Crypto.Cipher import AES, PKCS1_v1_5
        from Crypto.PublicKey import RSA
        self._b64 = base64
        self._AES = AES
        self._PKCS = PKCS1_v1_5
        pem = '-----BEGIN PUBLIC KEY-----\n' + '\n'.join((self._PEM_BODY[i:i + 64] for i in range(0, len(self._PEM_BODY), 64))) + '\n-----END PUBLIC KEY-----'
        self._key = RSA.import_key(pem)
        self.aes_key = ''.join((random.choice(self.CHARS) for _ in range(16)))
        self.aes_iv = ''.join((random.choice(self.CHARS) for _ in range(16)))

    def _c(self):
        return self._AES.new(self.aes_key.encode(), self._AES.MODE_CBC, self.aes_iv.encode())

    def encrypt(self, params: Dict[str, Any]) -> str:
        plain = json.dumps(params, separators=(',', ':'), ensure_ascii=False).encode()
        pad = 16 - len(plain) % 16
        body = self._c().encrypt(plain + bytes([pad]) * pad)
        rsa_ct = self._PKCS.new(self._key).encrypt(self.aes_key.encode())
        buf = struct.pack('>h', self.PROTOCOL_TYPE) + struct.pack('>h', self.MAGIC) + struct.pack('>h', self.KEY_VERSION) + bytes([self.ENC_TYPE]) + bytes([len(self.KEY_TOKEN)]) + self.KEY_TOKEN + self.aes_iv.encode() + struct.pack('>h', len(rsa_ct)) + rsa_ct
        crc = struct.pack('>Q', zlib.crc32(buf) & 4294967295) + buf
        blob = struct.pack('>H', len(crc) + 2) + crc + body
        return self._b64.b64encode(blob).decode().replace('+', '-').replace('/', '_').replace('=', '')

    def decrypt(self, text: str) -> str:
        t = text.replace('-', '+').replace('_', '/')
        t = ''.join((c for c in t if c.isalnum() or c in '+/'))
        raw = self._b64.b64decode(t + '=' * (-len(t) % 4))
        tok_len = raw[17]
        total = tok_len + 34
        key_len = struct.unpack('>h', raw[total:total + 2])[0]
        body = raw[total + 2 + key_len:]
        plain = self._c().decrypt(body + b'\x00' * (-len(body) % 16))
        return plain[:-plain[-1]].decode('utf-8', 'replace')

def log(*args, **kwargs):
    return None
