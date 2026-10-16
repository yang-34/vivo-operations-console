"""Account-bound vivo protocol profiles; generated profiles are not native sessions."""
from __future__ import annotations

import re
import secrets
import uuid
from datetime import datetime, timezone

from .common import gen_imei, gen_oaid

# Model codes and physical resolutions: https://www.vivo.com.cn/vivo/param/<slug>.
# Android build IDs below are protocol compatibility values, not captured firmware.
MODEL_CATALOG = (
    ('V2241A', 'vivo X90', '13', '1260_2800', 'PD2241', 'x90'),
    ('V2242A', 'vivo X90 Pro', '13', '1260_2800', 'PD2242', 'x90pro'),
    ('V2227A', 'vivo X90 Pro+', '13', '1440_3200', 'PD2227', 'x90proplus'),
    ('V2309A', 'vivo X100', '14', '1260_2800', 'PD2309', 'x100'),
    ('V2324A', 'vivo X100 Pro', '14', '1260_2800', 'PD2324', 'x100pro'),
    ('V2366GA', 'vivo X100 Ultra', '14', '1440_3200', 'PD2366G', 'x100ultra'),
)
BUILDS = {'13': 'TP1A.220624.014', '14': 'UP1A.231005.007'}
PUBLIC_VIVO = {'brand': 'vivo', 'manufacturer': 'vivo', 'model': 'V2227A',
               'android_ver': '13', 'sdk_int': 33, 'density': '3.0',
               'screensize': '1080_2400', 'net_type': 'wifi'}
IDENTIFIERS = ('imei', 'oaid', 'vaid', 'aaid', 'ec', 'ufs_id', 'wallet_device_id')
DEVICE_KEYS = (*PUBLIC_VIVO, *IDENTIFIERS, 'build_id', 'market_name', 'product_name',
               'resolution', 'device_type', 'chrome_version')


def original_device(raw):
    original = {key: raw[key] for key in DEVICE_KEYS if raw.get(key) not in (None, '')}
    original.update(raw.get('device') or {})
    return original


def is_original_vivo(device):
    brands = [str(device[key]).lower() for key in ('brand', 'manufacturer') if device.get(key)]
    return (not any(brand not in ('vivo', 'iqoo') for brand in brands)
            and bool(re.fullmatch(r'[VvIi][0-9]{4}[A-Za-z]*', str(device.get('model') or '')))
            and device.get('profile_source') not in ('assigned-vivo', 'vivo-protocol-default'))


def ensure_device_profile(raw, identity='', *, preserve_legacy=False):
    """Assign once on import/migration, never on an HTTP request or account read."""
    original = original_device(raw)
    if raw.get('protocol_device') or (is_original_vivo(original) and all(
            original.get(key) for key in ('model', 'android_ver', 'sdk_int'))):
        return raw
    model, name, android, screen, product, slug = secrets.choice(MODEL_CATALOG)
    seed = str(raw.get('openid') or identity) if preserve_legacy else uuid.uuid4().hex
    profile = dict(brand='vivo', manufacturer='vivo', model=model, market_name=name,
                   android_ver=android, sdk_int=int(android) + 20, screensize=screen,
                   resolution=screen, product_name=product, build_id=BUILDS[android],
                   density=secrets.choice(('3.0', '3.5', '4.0')), net_type='wifi',
                   chrome_version='120.0.0.0' if android == '13' else '124.0.0.0',
                   device_type='phone', profile_source='assigned-vivo', native_verified=False,
                   assignment_id=uuid.uuid4().hex, assigned_at=datetime.now(timezone.utc).isoformat(),
                   specification_url='https://www.vivo.com.cn/vivo/param/' + slug)
    # Preserve IDs already used by the existing client, including its legacy derivation.
    for key, generator in (('imei', gen_imei), ('oaid', gen_oaid)):
        profile[key] = raw.get(key) or original.get(key) or generator(seed)
    return {**raw, 'protocol_device': profile}


def resolve_device(raw):
    original = original_device(raw)
    assigned = raw.get('protocol_device') or {}
    device = {**PUBLIC_VIVO, **assigned}
    native = is_original_vivo(original)
    if native:
        device.update({key: value for key, value in original.items() if value not in (None, '')})
        if original.get('android_ver') and not original.get('sdk_int'):
            device['sdk_int'] = int(original['android_ver']) + 20
        elif original.get('sdk_int') and not original.get('android_ver'):
            device['android_ver'] = str(int(original['sdk_int']) - 20)
    for key in IDENTIFIERS:
        if raw.get(key) or original.get(key):
            device[key] = raw.get(key) or original[key]
    device.update(brand='vivo', manufacturer='vivo')
    device['native_verified'] = bool(native and all(original.get(key) for key in ('model', 'android_ver', 'sdk_int')))
    device['profile_source'] = 'original-vivo' if native else ('assigned-vivo' if assigned else 'vivo-protocol-default')
    device.setdefault('build_id', BUILDS.get(str(device['android_ver']), ''))
    return device


def webview_ua(device):
    build = f" Build/{device['build_id']}" if device.get('build_id') else ''
    chrome = device.get('chrome_version', '120.0.0.0')
    return (f"Mozilla/5.0 (Linux; Android {device['android_ver']}; {device['model']}{build}; wv) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/{chrome} Mobile Safari/537.36")


def wap_ua(device):
    return webview_ua(device).replace('; wv)', ')').replace('Version/4.0 ', '')
