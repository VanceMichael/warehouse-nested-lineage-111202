from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal


def overview(connection):
    rows = [dict(record) for record in connection.execute('SELECT order_id, quantity, price FROM purchase_lines ORDER BY line')]
    totals = defaultdict(lambda: Decimal("0"))
    for record in rows:
        key = tuple(record[field] for field in ['order_id'])
        value = Decimal(record['quantity']) * Decimal(record['price'])
        if not value.is_finite():
            raise ValueError("数量必须为有限十进制")
        totals[key] += value
    return [dict(zip(['order_id'], key), **{'amount': str(value)}) for key, value in sorted(totals.items())]
