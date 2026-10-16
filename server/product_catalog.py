"""Group city-specific catalogue rows for display, preserving redemption IDs."""
import json
from collections import defaultdict

from store import db


def metadata(row):
    try:
        value = json.loads(row.get('metadata') or '{}')
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def family_key(row):
    item_id = metadata(row).get('itemId')
    # Point and EC feeds can return the same platform SKU as well as many cities.
    # Titles alone cannot identify a SKU (different offers may share a title).
    return ('item', str(item_id)) if item_id is not None and str(item_id) else ('local', row['id'])


def city(row):
    return str(metadata(row).get('city') or '全国')


def available_stock(row):
    return max(0, int(row['stock'])) if row.get('remote_active', 1) else 0


def offer_order(row):
    return (available_stock(row) <= 0, row['price'],
            str(metadata(row).get('channel') or 'point').lower() != 'point',
            -available_stock(row), row['id'])


def city_offers(rows):
    by_city = defaultdict(list)
    for row in rows:
        by_city[city(row)].append(row)
    # Repeated feeds do not represent additional stock. Prefer the available,
    # lowest priced offer in each city, using the standard feed to break ties.
    return [min(offers, key=offer_order) for _, offers in sorted(
        by_city.items(), key=lambda item: (item[0] != '全国', item[0]))]


def public_offer(row):
    return {'id': row['id'], 'city': city(row), 'price': row['price'],
            'stock': available_stock(row)}


def product_card(rows):
    offers = city_offers(rows)
    available = [row for row in offers if available_stock(row) > 0]
    priced = available or offers
    representative = min(priced, key=offer_order)
    item = {key: value for key, value in representative.items() if key != 'metadata'}
    item.update(city=city(representative), variant_count=len(offers),
                city_count=len(offers), available_city_count=len(available),
                has_city_variants=any(city(row) != '全国' for row in offers),
                price=min(row['price'] for row in priced),
                price_max=max(row['price'] for row in priced),
                # The same upstream stock may appear under hundreds of cities.
                stock=max(available_stock(row) for row in offers))
    return item


def product_families(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[family_key(row)].append(row)
    return list(grouped.values())


def catalogue_page(mode, page, size, *, category='all', source='all', query='', in_stock=False, sort='default'):
    with db(mode) as connection:
        rows = [dict(row) for row in connection.execute('SELECT * FROM products ORDER BY rowid')]
    categories = list(dict.fromkeys(row['category'] for row in rows))
    query = query.strip().casefold()
    cards = []
    for family in product_families(rows):
        if category not in ('', 'all') and not any(row['category'] == category for row in family):
            continue
        if source not in ('', 'all') and not any(row['source'] == source for row in family):
            continue
        if query and not any(query in (row['name'] + ' ' + row['description'] + ' ' + city(row)).casefold() for row in family):
            continue
        card = product_card(family)
        if not in_stock or card['stock'] > 0:
            cards.append(card)
    if sort == 'price_asc':
        cards.sort(key=lambda row: row['price'])
    elif sort == 'price_desc':
        cards.sort(key=lambda row: row['price'], reverse=True)
    elif sort == 'stock':
        cards.sort(key=lambda row: row['stock'], reverse=True)
    start = (page - 1) * size
    return {'items': cards[start:start + size], 'total': len(cards), 'categories': categories}


def variants(mode, product_id):
    with db(mode) as connection:
        product = connection.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
        if product is None:
            return None
        key = family_key(dict(product))
        family = [dict(row) for row in connection.execute('SELECT * FROM products')
                  if family_key(dict(row)) == key]
    return {'items': [public_offer(row) for row in city_offers(family)]}
