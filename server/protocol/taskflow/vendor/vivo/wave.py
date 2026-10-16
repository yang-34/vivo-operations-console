# Bundled vivo protocol reference; see source-manifest.json.
"""vivo wave 签名族 —— 纯 Python 实现（已用 unidbg 逐字节验证）。

来源（全部为静态分析所得，非猜测）：

* `libvivo_account_wave.so` 导出 3 个 JNI 方法：
  - ``Java_com_vivo_md5_Wave_waveString``
  - ``Java_com_vivo_md5_Wave_waveStringEnd``
  - ``Java_com_vivo_md5_Wave_waveStringNet``
* `libvivosgmain.so` 内含**完全相同的两个常量**（见下），证明两库实现同一算法。
* Java 侧包装：
  - ``com.vivo.md5.Wave.a(Context, ArrayList<String>)``
  - ``com.vivo.security.Wave.getSign(Context, Object...)``（安全 SDK，产出 m 值 / sign）

反汇编结论（ARM64，`libvivo_account_wave.so`）：

``waveString(s)``      = md5_hex(s)                                  # 0x1e9c
``waveStringEnd(s)``   = md5_hex(s + SUFFIX_132)                     # 0x1fb4
``waveStringNet(a,b)`` = (int(rev[7:14],16) + int(rev[14:24],16)) & 0xFFFFFFFF
                         其中 rev = reverse(md5_hex(PREFIX_361 + b))    # 0x210c
                         注意：**取第 2 个参数**（第 1 个是 Context，native 忽略）

验证：6/6 样本与 unidbg 仿真输出逐字节一致（见 `04_脚本/selftest_wave.py`）。
"""
from __future__ import annotations
import hashlib
from urllib.parse import quote, unquote
NET_PREFIX = 'R7cF8SoztcIzrECAIiz%2F0buRl2GGnQF4QJwyuINtTLWSIjNI9TsfuvNQTxiq%2BP0yAVt1ZRSyy5AsSfeNGwoa6AmPkwxjpni6quiOuuID1wZbrrj2PvG9%2B069AZLTu%2Fyr0luWfhhKDPGtYpuk4xBZrHvsbVvgjHGwqY6zIQFFGy6lNVfTvKV%2BMqOVHM8X6Qiphp2ckuQfZd5oqXcQSpUwEC8GnzKSv2XA7QfOG4kdVwxu9WNAUas0fky1Cq%2F%2Bj%2BttR6TRzWwBFMQZEX02LTQn6YyB6LyftWv%2F0YPlU6Hh38scNSTeTaOW1j8vMxdGB6bWg1fZNIq3T7t%2FU3W%2FQyz'
END_SUFFIX = 'AAAAB3NzaC1yc2EAAAADAQABAAABAQDHSGIpA2xfTLJLcbktC3Gpx7smi4qvzuo29PxBDEqi7Gqek7cm3gshhkOK%2F6VHfVXj%2Bdf6OdAJNR8KPx0HuSUPafo4P3t1Bf1n'
ENCODE_MODE = 'utf-8'
SIGN_VERSION = '2'
FORUM_SIG_KEY = 'vivospaceprivkey=j56Hi8o1xzu6Gp'

def wave_string(s: str) -> str:
    """`waveString(s)` = md5(s) 的小写 hex。"""
    return hashlib.md5(s.encode('utf-8')).hexdigest()

def wave_string_end(s: str) -> str:
    """`waveStringEnd(s)` = md5(s + END_SUFFIX) 的小写 hex。"""
    return hashlib.md5((s + END_SUFFIX).encode('utf-8')).hexdigest()

def wave_string_net(arg1: str, arg2: str) -> int:
    """`waveStringNet(context, b)` —— **只用第 2 个参数**，返回 32 位无符号整数。

    第 1 个参数在 Java 签名里是 ``Context``，native 侧不读它（反汇编 0x2110
    ``mov x1, x3`` 直接把第 4 个寄存器参数当输入），因此这里保留形参只为对齐语义。
    """
    rev = hashlib.md5((NET_PREFIX + arg2).encode('utf-8')).hexdigest()[::-1]
    return int(rev[7:14], 16) + int(rev[14:24], 16) & 4294967295

def get_utf8_string_only(s: str) -> str:
    """复刻 ``com.vivo.security.Wave.getUtf8StringOnly``：去掉 4 字节 UTF-8 序列（emoji）。

    实现与源码一致：按 UTF-8 逐字节扫描，遇到 ``b & 0xF8 == 0xF0`` 且剩余 >= 4 字节时
    跳过 4 字节。
    """
    raw = s.encode('utf-8')
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

def get_sign(param_values) -> str:
    """复刻 ``com.vivo.security.Wave.getSign(Context, Object...)``。

    * 入参是一批**已排序**的字符串（调用方 `getMValue` 会先 ``Collections.sort``）
    * 拼成 ``":" + v1 + ":" + v2 ...``
    * 走 ``waveStringNet``，前面加 ``"2|"``，最后 URL 编码（utf-8）

    返回示例：``2%7C3599230318``
    """
    sb = ''.join((':' + str(v) for v in param_values))
    inner = wave_string_net('', get_utf8_string_only(sb))
    return quote(f'{SIGN_VERSION}|{inner}', safe='')

def m_value_for_post(param_map: dict, extra_url: str='') -> str:
    """复刻 ``com.vivo.security.Wave.getValueForPostRequest(ctx, url, map)``。

    算法（源码 `com/vivo/security/Wave.java`）：

    1. ``getPValue(map, false)``：取 map 的**全部 value**（不排序、不取 key）
    2. ``parseUrl(url)``：若 url 含 ``?`` 取问号后部分；按 ``&`` 拆，按 ``=`` 拆，
       取第 2 段（值）并 URLDecode；**同名 key 会去重**
    3. 两批值合并 → ``Collections.sort`` 字典序排序
    4. ``getSign`` → ``"2|" + waveStringNet(...)`` 再 URL 编码

    :param param_map: 业务参数（**调用方须在调用本函数之前把除 sign 外的参数都放好**）
    :param extra_url: 参与签名的"URL"（如 ``vivospaceprivkey=j56Hi8o1xzu6Gp``），
                      可以为空字符串
    """
    values = ['' if v is None else str(v) for v in param_map.values()]
    values.extend(_parse_url_values(extra_url))
    values.sort()
    return get_sign(values)

def m_value_for_get(full_url: str) -> str:
    """复刻 ``com.vivo.security.Wave.getValueForGetRequest(ctx, url)``。

    只对 URL 的 query 部分取值（同名 key 去重），排序后签名。
    """
    values = _parse_url_values(full_url)
    values.sort()
    return get_sign(values)

def m_value_for_cookies(cookie_map: dict) -> str:
    """复刻 ``com.vivo.security.Wave.getValueForCookies(ctx, map)``。

    与 POST 的差别：**每个 value 先做一次 URLDecode**（源码 ``getPValue(map, true)``）。
    """
    values = []
    for v in cookie_map.values():
        v = '' if v is None else str(v)
        try:
            v = unquote(v)
        except Exception:
            pass
        values.append(v)
    values.sort()
    return get_sign(values)

def md5_wave_sign(param_values) -> str:
    """复刻 ``com.vivo.md5.Wave.a(Context, ArrayList<String>)``（**账号侧**签名）。

    与 :func:`get_sign` 的差别：**没有** ``getUtf8StringOnly``（不剔除 emoji）。

    源码链路（`com/vivo/md5/Wave.java:18-50` + `passport/Z.java:122-134`）::

        Collections.sort(values)
        s = ":" + ":".join(values)
        URLEncoder.encode("2|" + waveStringNet(ctx, s), "utf-8")   # Wave.a
        URLDecoder.decode(...)                                     # Z.java 里又解回来

    两次编解码相抵 ⇒ 返回值就是字面量 ``"2|<十进制数>"``。

    ⚠️ 调用方必须保证 ``param_values`` 是**除 ``s`` 之外**全部参数的值。
    """
    values = ['' if v is None else str(v) for v in param_values]
    values.sort()
    inner = wave_string_net('', ':' + ':'.join(values))
    return f'{SIGN_VERSION}|{inner}'

def _parse_url_values(url: str) -> list:
    """复刻 ``com.vivo.security.Wave.parseUrl``：返回 query 中**去重后**的值列表。"""
    if not url:
        return []
    s = url
    if '?' in s:
        s = s.split('?', 1)[1]
    if not s or '=' not in s:
        return []
    out: list = []
    seen: dict = {}
    for part in s.split('&'):
        kv = part.split('=')
        if len(kv) > 1:
            try:
                val = unquote(kv[1])
            except Exception:
                val = kv[1]
            if kv[0] in seen:
                try:
                    out.remove(seen[kv[0]])
                except ValueError:
                    pass
            else:
                out.append(val)
            seen[kv[0]] = val
        else:
            if kv[0] in seen:
                try:
                    out.remove(seen[kv[0]])
                except ValueError:
                    pass
            else:
                out.append('')
            seen[kv[0]] = ''
    return out
