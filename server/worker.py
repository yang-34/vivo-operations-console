"""Compatibility exports. Production execution uses the built-in native pipeline."""
import re
from protocol.runner import classify


def parse_pool_urls(value):
    if isinstance(value,str):value=re.split(r'[|;\r\n]+',value)
    return list(dict.fromkeys(str(url).strip() for url in value if str(url).strip()))


def load_reference(root=None):
    """Legacy diagnostic name; never imports or reads anything from root."""
    import protocol.api as module
    return module
