# Bundled vivo protocol reference; see source-manifest.json.
"""Resolve the official store's map position without inventing user GPS."""
import math
from urllib.parse import parse_qs, urlencode, urlparse
from .added_common import checked, obj, rows, title
from .protocol_common import ActionRequired, ProtocolError
MAP_HOST = 'https://mall.vivo.com.cn'
DEFAULT_CITY = '北京市'

def coordinates(lng, lat):
    try:
        if isinstance(lng, bool) or isinstance(lat, bool):
            raise ValueError
        lng, lat = (float(lng), float(lat))
        if not math.isfinite(lng) or not math.isfinite(lat):
            raise ValueError
        if not -180 <= lng <= 180 or not -90 <= lat <= 90:
            raise ValueError
    except (TypeError, ValueError):
        raise ProtocolError('门店地图经纬度格式无效。') from None
    return (lng, lat)

def map_coordinates(link):
    """Decode a map marker locally; no task-supplied URL is fetched."""
    parsed = urlparse(str(link))
    if parsed.scheme not in ('https', 'http') or parsed.username or parsed.password:
        raise ProtocolError('门店地图链接格式无效。')
    query = parse_qs(parsed.query)
    if parsed.hostname == 'uri.amap.com':
        value = query.get('position', query.get('to', ['']))[0].split(',')
        if len(value) >= 2:
            return coordinates(value[0], value[1])
    elif parsed.hostname == 'apis.map.qq.com':
        value = query.get('marker', [''])[0]
        if value.startswith('coord:'):
            value = value[6:].split(';', 1)[0].split(',')
            if len(value) == 2:
                return coordinates(value[1], value[0])
    raise ProtocolError('门店地图没有明确的目标坐标，请使用官方门店地图标记。')

def city_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 60:
        raise ProtocolError('门店城市资料格式无效。')
    if any((ord(c) < 32 for c in value)):
        raise ProtocolError('门店城市资料格式无效。')
    return value.removesuffix('城区').removesuffix('市')

def directory(client, supplied):
    city = supplied.get('city') or DEFAULT_CITY
    city_name(city)
    fields = {'province': supplied.get('province') or (city if city in ('北京市', '天津市', '上海市', '重庆市') else None), 'city': city, 'firstPageSize': 20, 'otherPageSize': 20, 'pageId': 1, 'channelCode': 'PC_STORES'}
    fields = {k: v for k, v in fields.items() if v is not None}
    client.http.hosts |= {'mall.vivo.com.cn'}
    response = client.http.post(MAP_HOST + '/api/vhome/store/shops', json=fields, headers={'User-Agent': client.ua, 'Referer': 'https://www.vivo.com.cn/store'})
    try:
        data = obj(checked(response.json(), operation='查询官方门店地图').get('data'))
    except ValueError as error:
        raise ProtocolError('官方门店地图响应无法识别。') from error
    if not isinstance(data.get('list'), list):
        raise ProtocolError('官方门店地图缺少当前门店清单。')
    stores = rows(data['list'])
    sid, code = (supplied.get('store_id'), supplied.get('store_code'))
    if sid or code:
        selected = next((s for s in stores if (not sid or str(s.get('thirdOrganizationId')) == str(sid)) and (not code or s.get('storeCode') == code)), None)
        if not selected:
            raise ActionRequired('指定门店不在当前城市的地图目录中，请补充该店地图位置或对应城市。', reason_kind='missing_context')
    else:
        selected = next((s for s in stores if s.get('organizationType') == 'TZ'), None)
        selected = selected or next(iter(stores), None)
    return selected

def resolve(client, supplied):
    device = obj(client.credentials.get('device'))
    lng = supplied.get('longitude', device.get('longitude'))
    lat = supplied.get('latitude', device.get('latitude'))
    city = supplied.get('city', device.get('city'))
    if lng is not None and lat is not None and city:
        lng, lat = coordinates(lng, lat)
        return ({'longitude': lng, 'latitude': lat, 'city': city_name(city)}, None, 'user_location')
    store = obj(supplied.get('store'))
    link = supplied.get('store_map_url')
    if link:
        lng, lat = map_coordinates(link)
        if not city:
            raise ActionRequired('该门店地图标记缺少城市，请补充门店所在城市。', reason_kind='missing_context')
        return ({'longitude': lng, 'latitude': lat, 'city': city_name(city)}, store or None, 'store_map')
    if not store:
        store = directory(client, supplied)
    if not store:
        return (None, None, 'store_map')
    lng, lat = coordinates(store.get('longitude'), store.get('latitude'))
    city = city_name(store.get('city') or city)
    return ({'longitude': lng, 'latitude': lat, 'city': city}, store, 'store_map')

def presentation(store, location, source):
    return {'name': title(store.get('name', store.get('externalName')), '门店'), 'address': title(store.get('address'), '官方未提供地址'), **location, 'source': source, 'map_url': 'https://uri.amap.com/marker?' + urlencode({'position': f"{location['longitude']},{location['latitude']}", 'name': title(store.get('name', store.get('externalName')), '门店'), 'coordinate': 'gaode', 'callnative': '0'})}
