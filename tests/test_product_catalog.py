import json

from store import db, now


LIVE = {'X-Data-Mode': 'live'}


def add_offer(pid, sku, city, *, name='同款礼券', price=5, stock=10, channel='point', active=1, mode='live'):
    metadata = {'itemId': sku, 'city': city, 'vkCityId': f'city-{city}', 'channel': channel,
                'private_field': 'not-public-catalog-data'}
    with db(mode) as connection:
        connection.execute('''INSERT INTO products(id,name,category,price,stock,description,metadata,source,updated_at,remote_active)
            VALUES(?,?,'测试礼券',?,?,?,?,?,?,?)''',
            (pid, name, price, stock, city+' · 测试商品', json.dumps(metadata), 'live', now(), active))


def test_city_duplicates_are_grouped_before_pagination_and_not_counted_as_stock(client):
    add_offer('city-a:sku-a', 'sku-a', '示例甲城')
    add_offer('city-a:sku-b', 'sku-b', '示例甲城', name='另一款礼券', price=20)
    add_offer('city-b:sku-a', 'sku-a', '示例乙城')
    add_offer('national:ec:sku-a', 'sku-a', '示例甲城', channel='EC')
    first = client.get('/api/products?page_size=1&page=1', headers=LIVE).json
    second = client.get('/api/products?page_size=1&page=2', headers=LIVE).json
    assert first['total'] == second['total'] == 2
    assert first['items'][0]['name'] == '同款礼券'
    assert second['items'][0]['name'] == '另一款礼券'
    card = first['items'][0]
    assert card['city_count'] == card['available_city_count'] == 2
    assert card['stock'] == 10  # Not 30: repeated feeds can share stock.
    assert 'metadata' not in card and 'not-public-catalog-data' not in json.dumps(first)


def test_city_search_keeps_all_city_choices_and_their_individual_prices(client):
    add_offer('a:sku', 'sku', '示例甲城', price=5, stock=0)
    add_offer('b:sku', 'sku', '示例乙城', price=8, stock=3)
    add_offer('c:sku', 'sku', '示例丙城', price=12, stock=6)
    add_offer('b:ec:sku', 'sku', '示例乙城', price=8, stock=3, channel='EC')
    response = client.get('/api/products?q=示例甲城&stock=1', headers=LIVE).json
    assert response['total'] == 1
    product = response['items'][0]
    assert product['price'] == 8 and product['price_max'] == 12
    assert product['variant_count'] == 3 and product['available_city_count'] == 2
    offers = client.get('/api/products/'+product['id']+'/variants', headers=LIVE).json['items']
    cities = {offer['city']: offer for offer in offers}
    assert len(cities) == 3
    assert cities['示例甲城']['stock'] == 0
    assert cities['示例乙城'] == {'id': 'b:sku', 'city': '示例乙城', 'price': 8, 'stock': 3}
    assert cities['示例丙城']['price'] == 12
    assert all('metadata' not in offer for offer in offers)
    assert client.get('/api/products/missing/variants', headers=LIVE).status_code == 404


def test_same_title_with_different_skus_stays_separate_and_inactive_stock_is_unavailable(client):
    add_offer('a:one', 'one', '示例甲城', price=5, stock=0)
    add_offer('b:one', 'one', '示例乙城', price=1, stock=99, active=0)
    add_offer('a:two', 'two', '示例甲城', price=10)
    assert client.get('/api/products', headers=LIVE).json['total'] == 2
    available = client.get('/api/products?stock=1&sort=price_asc', headers=LIVE).json
    assert available['total'] == 1 and available['items'][0]['id'] == 'a:two'
    prices = [p['price'] for p in client.get('/api/products?sort=price_desc', headers=LIVE).json['items']]
    assert prices == [10, 1]


def test_missing_or_invalid_metadata_does_not_merge_unrelated_local_products(client):
    with db('live') as connection:
        for pid, metadata in [('one', '{}'), ('two', '{broken'), ('three', 'null')]:
            connection.execute("INSERT INTO products(id,name,category,price,stock,metadata,updated_at) VALUES(?,'同名本地商品','本地',5,1,?,?)", (pid, metadata, now()))
    assert client.get('/api/products', headers=LIVE).json['total'] == 3
    assert client.get('/api/products/one/variants', headers=LIVE).json['items'] == [
        {'id': 'one', 'city': '全国', 'price': 5, 'stock': 1}]


def test_selected_city_redemption_uses_its_price_and_only_reserves_its_stock(client):
    add_offer('demo-city-a', 'demo-sku', '示例甲城', price=5, stock=9, mode='demo')
    add_offer('demo-city-b', 'demo-sku', '示例乙城', price=12, stock=4, mode='demo')
    with db('demo') as connection:
        before = connection.execute('SELECT points FROM accounts WHERE id=1').fetchone()[0]
    result = client.post('/api/redemptions', json={'account_id': 1, 'product_id': 'demo-city-b', 'idempotency_key': 'selected-city'}).json
    assert result['status'] == 'success' and result['product_id'] == 'demo-city-b' and result['price'] == 12
    with db('demo') as connection:
        assert connection.execute('SELECT points FROM accounts WHERE id=1').fetchone()[0] == before - 12
        stocks = dict(connection.execute("SELECT id,stock FROM products WHERE id LIKE 'demo-city-%'"))
        assert stocks == {'demo-city-a': 9, 'demo-city-b': 3}
