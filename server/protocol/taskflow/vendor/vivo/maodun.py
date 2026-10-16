# Bundled vivo protocol reference; see source-manifest.json.
"""vivo H5 「矛盾」(MaodunCipher) 信封 —— 纯 Python 全自动实现。

## 为什么需要它

积分商城 H5（`pointh5.vivo.com.cn` / `pointstore.vivo.com.cn`）在**正式环境**里对
**抽奖 / 兑换 / 积分抵现** 三个写接口做**参数加密**（源码位置：
`pointwsdl.vivo.com.cn/point/static/js/maodun/prod/maodun.js` 的
`p.interceptors.request.use`）：

```js
if(url.indexOf("api/mall/item/draw.do")>-1 || exchange || payWithCash)
   && window.MaodunCipher) {
     var t = MaodunCipher.encrypt(JSON.stringify(e.params));
     if (t.code == 0) e.params = { jvq_param: t.data };
}
```

即：**明文参数被丢弃**，整包换成 `jvq_param=<加密串>`，且该 `jvq_param` 走 **query**。
这就是为什么直接发明文参数会得到 `12183 当前业务端不支持`——服务端根本没解出参数。

## 协议结构（逐字节从 `maodun.js` 还原，全部**大端**）

载荷（magic **203**，以下偏移相对**整个包**）：

```
[0:2]        总长度 = 2 + len(crcParams)
[2:10]       CRC32（8 字节大端；CRC 只算 [10:] 即 buffenParams）
[10:12]      protocalType  = 0x0020
[12:14]      magic         = 203
[14:16]      keyVersion    = 0x0001
[16:17]      encryptionType= 0x41
[17:18]      tokenLen      = 19
[18:18+N]    keyToken      = "com.vivo.point.mall"
[37:53]      aesIv         = 随机 16 字符的 ASCII（16 字节）
[53:55]      rsaKeyLen     = RSA 密文长度（大端 int16）
[55:55+L]    rsaKey        = RSA_PKCS1v15(aesKey) 原始密文字节
[55+L:]      AES-128-CBC/PKCS7(JSON.stringify(params)) 密文
```

> 响应解密用 `subarray(12,14)` 判 203、`subarray(17,18)` 取 tokenLen、
> `totalLength = tokenLen + 34` —— 与上表完全对应。

外层编码：标准 base64 → URL-safe（`+`→`-`、`/`→`_`）→ 去掉 `=`。

## 密钥来源

* `aesKey` / `aesIv`：**每次页面加载随机**，各 16 字符，字符集 `0-9A-Z`
  （`MaodunCipher.randomGenerate`）。
* RSA 公钥：**静态**，明文写在协议头 `publicKeyProtocolHeaderParams`（magic 204）里，
  见下方 `RSA_PUBLIC_PEM`。

## 校验

`MaodunCipher.selftest_offline()` 做过：包长/CRC/magic 布局自洽，
且 `AES(key,iv)` 自解回明文。
"""
from __future__ import annotations
import base64
import json
import random
import struct
import zlib
from typing import Any, Dict, Optional, Tuple
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
MAGIC_REQUEST = 203
MAGIC_HEADER = 204
PROTOCOL_TYPE = 32
KEY_VERSION = 1
ENCRYPTION_TYPE = 65
KEY_TOKEN = b'com.vivo.point.mall'
RANDOM_CHARS = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
_PEM_BODY = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAhYDpLPqfVnPs2zV6IPTBt743XC8vNSiuqaQXopavStHuTgopj7iuwvPCGae7d1tB0stu6lVIFG/NBcQeOz308+AI5kcdIfA5HNri4vlT0cUCDTs02l/BEsZRGuk1MhL5ky+ka00JzVXbS4Or6QnMHrX6H/vmrDsbsyiCuO7NZj6zZA3VWov2GO7UaziuMgmrMYcT1WvRK9P3NMMaA4oZrA5+yhNCrYXFaSOmJGNOpSBS8LMI+aGOsjVyoJdBrhdrwaQuKALQ3jAqNmN+6OdJR04JIxpCNJUauCiKarWwTOjD7F6UdNSd2N6N/YMIYlA6A6xCC+ot9Y7uD3VsejKcuQIDAQAB'
RSA_PUBLIC_PEM = '-----BEGIN PUBLIC KEY-----\n' + '\n'.join((_PEM_BODY[i:i + 64] for i in range(0, len(_PEM_BODY), 64))) + '\n-----END PUBLIC KEY-----'
_RSA_KEY = RSA.import_key(RSA_PUBLIC_PEM)

def _rand16() -> str:
    """`MaodunCipher.randomGenerate(16)`：从 `0-9A-Z` 取 16 个字符。"""
    return ''.join((random.choice(RANDOM_CHARS) for _ in range(16)))

def _urlsafe_b64(data: bytes) -> str:
    return base64.b64encode(data).decode().replace('+', '-').replace('/', '_').replace('=', '')

def _urlsafe_unb64(text: str) -> bytes:
    t = text.replace('-', '+').replace('_', '/')
    t = ''.join((c for c in t if c.isalnum() or c in '+/'))
    return base64.b64decode(t + '=' * (-len(t) % 4))

def _int16_be(n: int) -> bytes:
    return struct.pack('>h', n)

class MaodunCipher:
    """一次会话一对 (aesKey, aesIv)；`encrypt`/`decrypt` 成对使用。"""

    def __init__(self, aes_key: Optional[str]=None, aes_iv: Optional[str]=None):
        self.aes_key = aes_key or _rand16()
        self.aes_iv = aes_iv or _rand16()

    def _new_cipher(self):
        return AES.new(self.aes_key.encode(), AES.MODE_CBC, self.aes_iv.encode())

    def encrypt(self, params: Dict[str, Any]) -> str:
        """把参数字典打成 `jvq_param`（URL-safe base64 字符串）。"""
        plain = json.dumps(params, separators=(',', ':'), ensure_ascii=False).encode()
        pad = 16 - len(plain) % 16
        body = self._new_cipher().encrypt(plain + bytes([pad]) * pad)
        rsa_ct = PKCS1_v1_5.new(_RSA_KEY).encrypt(self.aes_key.encode())
        buf = _int16_be(PROTOCOL_TYPE) + _int16_be(MAGIC_REQUEST) + _int16_be(KEY_VERSION) + bytes([ENCRYPTION_TYPE]) + bytes([len(KEY_TOKEN)]) + KEY_TOKEN + self.aes_iv.encode() + _int16_be(len(rsa_ct)) + rsa_ct
        crc_params = struct.pack('>Q', zlib.crc32(buf) & 4294967295) + buf
        header = struct.pack('>H', len(crc_params) + 2)
        return _urlsafe_b64(header + crc_params + body)

    def decrypt(self, text: str) -> str:
        """解开服务端回包（magic 203）。"""
        raw = _urlsafe_unb64(text)
        if len(raw) < 18:
            raise ValueError('maodun: 回包过短')
        magic = struct.unpack('>H', raw[12:14])[0]
        if magic != MAGIC_REQUEST:
            raise ValueError('maodun: magic=%d != 203' % magic)
        tok_len = raw[17]
        total = tok_len + 34
        key_len = struct.unpack('>h', raw[total:total + 2])[0]
        body = raw[total + 2 + key_len:]
        plain = self._new_cipher().decrypt(body + b'\x00' * (-len(body) % 16))
        pad = plain[-1]
        return plain[:-pad].decode('utf-8', 'replace')

    @staticmethod
    def selftest_offline() -> Tuple[bool, str]:
        c = MaodunCipher('0123456789ABCDEF', 'FEDCBA9876543210')
        src = {'itemId': 31508, 'categoryId': 125, 'channel': 'point'}
        blob = c.encrypt(src)
        raw = _urlsafe_unb64(blob)
        hdr_len = struct.unpack('>H', raw[0:2])[0]
        crc = struct.unpack('>Q', raw[2:10])[0]
        ok_len = 10 < hdr_len < len(raw)
        ok_crc = crc == zlib.crc32(raw[10:hdr_len]) & 4294967295
        ok_magic = struct.unpack('>H', raw[12:14])[0] == 203
        ok_body = json.loads(c.decrypt(blob)) == src
        msg = 'hdrLen=%s crc=%s magic=%s body=%s  (hdr=%d total=%d)' % (ok_len, ok_crc, ok_magic, ok_body, hdr_len, len(raw))
        return (ok_len and ok_crc and ok_magic and ok_body, msg)
if __name__ == '__main__':
    ok, m = MaodunCipher.selftest_offline()
    print(('PASS ' if ok else 'FAIL ') + m)
