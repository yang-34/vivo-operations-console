# Bundled vivo protocol reference; see source-manifest.json.
"""Preserve account identifiers and complete the source client's public attributes."""
from copy import deepcopy
from .protocol_common import ProtocolError
PUBLIC_DEFAULTS = {'density': '3.0', 'screensize': '1080_2400', 'net_type': 'wifi'}

def normalized_device(device):
    result = deepcopy(device)
    for field, value in PUBLIC_DEFAULTS.items():
        if result.get(field) in (None, ''):
            result[field] = value
    try:
        modern = int(result.get('sdk_int', 0)) >= 29
    except (TypeError, ValueError) as error:
        raise ProtocolError('vivo 原有 Android 版本资料无法识别。', reason_kind='missing_context') from error
    if modern:
        for name in ('vaid', 'aaid'):
            result.setdefault(name, '')
    return result
