"""The cookie inventory must be complete without exporting credential values."""
import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('inspect_har_contracts',Path(__file__).resolve().parents[1]/'scripts/inspect_har_contracts.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_inventory_combines_har_cookie_array_and_partial_header():
    request={'cookies':[{'name':'vvc_openid','value':'private-user'},
                        {'name':'vivo_account_cookie_iqoo_vivotoken','value':'private-token'}],
             'headers':[{'name':'Cookie','value':'vvc_elapsedtime=123; vvc_openid=private-user'}]}
    names=module.cookie_names(request)
    assert names==['vivo_account_cookie_iqoo_vivotoken','vvc_elapsedtime','vvc_openid']
    assert all('private' not in value for value in names)


def test_inventory_handles_absent_and_empty_cookies():
    assert module.cookie_names({})==[]
    assert module.cookie_names({'cookies':[],'headers':[{'name':'cookie','value':''}]})==[]
