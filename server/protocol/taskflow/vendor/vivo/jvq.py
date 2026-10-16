# Bundled vivo protocol reference; see source-manifest.json.
"""jvq（vivo seckey）加密层 —— **纯 Python 复刻**。

## 组成（全部来自静态源码 + 真机 oracle 实测的密钥）

| 部件 | 来源 |
|---|---|
| AES-128-CBC + PKCS7 | `libvivoseckey.so`（真机 frida oracle 取到的密钥，见 `00_工具/frida/README.md`） |
| 信封 `ProtocolPackage` | `com/vivo/seckeysdk/utils/ProtocolPackage.java` + `oh/a.java`、`oh/c.java` |
| keyToken | `com/vivo/seckeysdk/utils/b.java#i()` —— **只由包名决定，与设备无关** |
| URL 拼装 | `SecurityKeyCipher.toSecurityUrlV2(url, 1)` + `ph/b.java#a()` |
| Base64 | `android.util.Base64.encodeToString(bytes, 11)` = URL-safe、**无填充**、不换行 |

## 信封字节布局（`oh/c.k()`，protocol v1）

```
[0:2]   u16 BE  头长 headerLen (= tokenLen + 16)
[2:10]  u64 BE  CRC32( header[10:] )
[10:12] u16 BE  协议版本 = 1
[12]    u8      token 字节长度
[13:13+n]       token(UTF-8)
[n+13:n+15] u16 BE keyVersion
[n+15]  u8      type（本项目 AES 加密用 5 = PKGTYPE_AES_ENCRYPT）
之后紧跟 body（= AES 密文）
```

⚠️ **字节序是大端**（`oh/f.java#d/c` 都是 `>>>8`/`>>>56` 起）——写错会得到
`加密需要的参数不能为空` 之类含糊的服务端报错。

## keyToken（`b.i()`）

```
keyVersion=1 -> "jnisgmain@"      + 包名
keyVersion=2 -> "jnisgmain_v2@"   + 包名
keyVersion=6 -> "jnisgmain_v6@"   + 包名
```
⇒ **同一 APK、同一包名 ⇒ token 相同 ⇒ 与设备/账号无关**，可纯协议、多账号复用。

## 用法

```python
from vivo.jvq import to_security_url
u2 = to_security_url("https://eden.vivo.com.cn/xxx?a=1&b=2")   # 返回带 ?jvq_param=… 的 URL
```
"""
from __future__ import annotations
import base64
import zlib
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
AES_KEYS: Dict[int, Tuple[str, str]] = {1: ('5da590863052089893199b2a901b6470', '047cd76d65d3b28b4ccc2c0246681aa6'), 2: ('836e75afddae728551ad22b2bae6ca57', '047cd76d65d3b28b4ccc2c0246681aa6'), 6: ('95b3ff271e75d6766b418d7e1a8d2641', '047cd76d65d3b28b4ccc2c0246681aa6')}
SO_ENCRYPT_PRE_PACKAGE = 'jnisgmain@'
PKGTYPE_AES_ENCRYPT = 5
DEFAULT_KEY_VERSION = 6

def key_token(key_version: int, pkg_name: str) -> str:
    """`com/vivo/seckeysdk/utils/b.java#i()` —— **只依赖包名，与设备无关**。"""
    if key_version == 1:
        return SO_ENCRYPT_PRE_PACKAGE + pkg_name
    if key_version == 2:
        return 'jnisgmain_v2@' + pkg_name
    if key_version == 6:
        return 'jnisgmain_v6@' + pkg_name
    raise ValueError(f'unsupported keyVersion: {key_version}')

def _u16(n: int) -> bytes:
    """`oh/f.java#d(short)`：**大端**（`(byte)(s>>>8), (byte)s`）。"""
    return int(n).to_bytes(2, 'big')

def _u64(n: int) -> bytes:
    """`oh/f.java#c(long)`：**大端**（从 `>>>56` 开始）。"""
    return int(n).to_bytes(8, 'big')

def build_package(token: str, key_version: int, type_: int, body: bytes) -> bytes:
    """`oh/c.k()` —— 头 + body。"""
    t = token.encode('utf-8')
    hlen = len(t) + 16
    h = bytearray(hlen)
    h[0:2] = _u16(hlen)
    h[10:12] = _u16(1)
    h[12] = len(t) & 255
    h[13:13 + len(t)] = t
    h[len(t) + 13:len(t) + 15] = _u16(key_version)
    h[len(t) + 15] = type_ & 255
    crc = zlib.crc32(bytes(h[10:])) & 4294967295
    h[2:10] = _u64(crc)
    return bytes(h) + body

def aes_encrypt(plaintext: bytes, key_version: int=DEFAULT_KEY_VERSION) -> bytes:
    """等价 `SDKCipherNative.execute(ENCRYPT, AES, data, keyVersion).output`。"""
    key, iv = AES_KEYS[key_version]
    cipher = AES.new(bytes.fromhex(key), AES.MODE_CBC, bytes.fromhex(iv))
    return cipher.encrypt(pad(plaintext, 16))

def aes_decrypt(ciphertext: bytes, key_version: int=DEFAULT_KEY_VERSION) -> bytes:
    key, iv = AES_KEYS[key_version]
    cipher = AES.new(bytes.fromhex(key), AES.MODE_CBC, bytes.fromhex(iv))
    return cipher.decrypt(ciphertext)

def seckey_bytes(data: bytes, *, key_version: int=DEFAULT_KEY_VERSION, pkg_name: str='com.vivo.space') -> bytes:
    """`SecurityKeyCipher.aesEncrypt(data)` 的完整产物（信封 + 密文）。"""
    body = aes_encrypt(data, key_version)
    return build_package(key_token(key_version, pkg_name), key_version, PKGTYPE_AES_ENCRYPT, body)

def b64_flag11(data: bytes) -> str:
    """`android.util.Base64.encodeToString(data, 11)`：URL-safe、无填充、不换行。"""
    return base64.urlsafe_b64encode(data).decode().rstrip('=')

def _split_query(query: str) -> List[Tuple[str, str]]:
    """按出现顺序解析 `a=1&b=2`（`ph/b.java#c()`：保留插入顺序、同名 key 不允许）。"""
    out: List[Tuple[str, str]] = []
    seen = set()
    for part in (query or '').split('&'):
        if not part:
            continue
        k, _, v = part.partition('=')
        if k in seen:
            raise ValueError(f'duplicate query key: {k}')
        seen.add(k)
        out.append((k, v))
    return out

def _join_params(pairs: List[Tuple[str, str]], *, urlencode_whole: bool) -> str:
    """`ph/b.java#a(map, sortFlag=false, urlEncodeWhole)`。

    * 每个 key/value 各自 URL 编码；
    * `&` 连接；
    * 再对整串做一次 URL 编码（`toSecurityUrlV2` 传 true）。
    """
    s = '&'.join((f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs))
    return quote(s, safe='') if urlencode_whole else s

def to_security_url(url: str, *, key_version: int=DEFAULT_KEY_VERSION, pkg_name: str='com.vivo.space', v2: bool=True) -> str:
    """`SecurityKeyCipher.toSecurityUrlV2(url, 1)` —— 返回带 `?jvq_param=` 的 URL。

    （`lp/c.java#c()` 即 `toSecurityUrlV2(URLDecoder.decode(url), 1)`）
    """
    base, sep, query = url.partition('?')
    if not sep or not query:
        raise ValueError('url 必须带 query（jvq 只加密 query 参数）')
    pairs = _split_query(query)
    plaintext = _join_params(pairs, urlencode_whole=v2)
    pkg = seckey_bytes(plaintext.encode('utf-8'), key_version=key_version, pkg_name=pkg_name)
    return f'{base}?jvq_param={b64_flag11(pkg)}'

def build_query_jvq(params: Dict[str, str], *, key_version: int=DEFAULT_KEY_VERSION, pkg_name: str='com.vivo.space') -> str:
    """给定有序参数，直接产出 `jvq_param=` 的值（便于 POST 前自拼 URL）。"""
    pairs = list(params.items())
    plaintext = _join_params(pairs, urlencode_whole=True)
    return b64_flag11(seckey_bytes(plaintext.encode('utf-8'), key_version=key_version, pkg_name=pkg_name))

def parse_package(data: bytes) -> Dict[str, object]:
    """拆开一个 jvq 包：返回 `{header_len, version, token, key_version, type, body}`。"""
    if len(data) < 13:
        raise ValueError('jvq 包太短')
    hlen = int.from_bytes(data[0:2], 'big')
    version = int.from_bytes(data[10:12], 'big')
    tlen = data[12]
    token = data[13:13 + tlen].decode('utf-8', 'replace')
    key_version = int.from_bytes(data[13 + tlen:15 + tlen], 'big')
    type_ = data[15 + tlen]
    return {'header_len': hlen, 'version': version, 'token': token, 'key_version': key_version, 'type': type_, 'body': data[hlen:], 'crc32': int.from_bytes(data[2:10], 'big')}

def decrypt_response(b64_text: str) -> bytes:
    """解开服务端返回的 jvq 密文（`jvq_response` / 直接就是包体 base64）。

    ★ 实测（2026-10-08）：服务端**接受**本模块加密的请求，并用**同一密钥**加密回包；
    本函数能把回包还原成明文 JSON。
    """
    txt = (b64_text or '').strip()
    raw = base64.urlsafe_b64decode(txt + '=' * (-len(txt) % 4))
    info = parse_package(raw)
    kv = int(info['key_version'])
    body = info['body']
    if info['version'] != 1:
        raise ValueError(f"不支持的协议版本: {info['version']}")
    pt = aes_decrypt(body, kv if kv in AES_KEYS else DEFAULT_KEY_VERSION)
    if pt and 1 <= pt[-1] <= 16:
        pt = pt[:-pt[-1]]
    return pt

def try_decrypt_json(text: str):
    """尽力把响应体解成 JSON（是 jvq 密文就解，不是就原样返回）。"""
    import json as _json
    try:
        return _json.loads(text)
    except Exception:
        pass
    try:
        return _json.loads(decrypt_response(text).decode('utf-8', 'replace'))
    except Exception:
        return {'_raw': text[:400]}
