from decimal import Decimal, InvalidOperation


def cents(value):
    if value is None or isinstance(value, bool):
        raise ValueError("Missing money value")
    try:
        amount = Decimal(str(value)) * 100
    except InvalidOperation:
        raise ValueError("Invalid money value") from None
    if not amount.is_finite() or amount != amount.to_integral_value() or amount < 0:
        raise ValueError("Money must be nonnegative whole cents")
    return int(amount)


def money(value):
    return f"${Decimal(value) / 100:,.2f}"
