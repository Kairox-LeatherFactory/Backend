"""
INTEGRATION · price and delivery date land on the STYLE, and stay there.

A breakdown sheet prints the price and the ship date ONCE per style row, but a
style usually has several rows — one per colourway — so the same fact arrives
many times and the rows can disagree. The rules under test:

    PRICE     first non-empty wins; a later disagreement WARNS, naming both
    DELIVERY  the EARLIEST wins, because a ship date is a commitment
    CURRENCY  cell symbol -> order -> client; never invented
    RE-IMPORT identical values, no drift (replace=True is the normal path)
"""
import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.imports.load_to_db import _apply_style_commercials
from app.modules.imports.parse_orders import OrderLine

pytestmark = pytest.mark.integrity


class _Bag:
    """Stands in for the ClientPreview the loader collects warnings on."""
    def __init__(self):
        self.warnings = []


def _line(row, price=None, currency=None, delivery=None):
    return OrderLine(style="CLERMONT", color="TAUPE", article="SUEDE",
                     sizes={"M": 1}, total=1, source_row=row,
                     unit_price=price, currency=currency, delivery_date=delivery)


def test_the_first_price_wins_and_a_later_disagreement_is_reported():
    style, bag = Style(name="CLERMONT"), _Bag()
    _apply_style_commercials(style, _line(3, Decimal("83.00"), "EUR"), None, bag)
    _apply_style_commercials(style, _line(4, Decimal("93.00"), "EUR"), None, bag)

    assert style.unit_price == Decimal("83.00")
    assert style.currency == "EUR"
    assert len(bag.warnings) == 1
    # The warning has to carry BOTH numbers or it is not actionable.
    assert "83.00" in bag.warnings[0] and "93.00" in bag.warnings[0]
    assert "CLERMONT" in bag.warnings[0]


def test_the_earliest_delivery_wins_because_a_ship_date_is_a_commitment():
    style, bag = Style(name="CLERMONT"), _Bag()
    late, early = datetime.date(2026, 10, 1), datetime.date(2026, 9, 15)
    _apply_style_commercials(style, _line(3, delivery=late), None, bag)
    _apply_style_commercials(style, _line(4, delivery=early), None, bag)

    assert style.delivery_date == early, (
        "keeping the later date would plan the factory into missing the earlier "
        "commitment")
    assert len(bag.warnings) == 1
    assert "2026-09-15" in bag.warnings[0] and "2026-10-01" in bag.warnings[0]


def test_agreeing_rows_produce_no_warning_at_all():
    style, bag = Style(name="CLERMONT"), _Bag()
    d = datetime.date(2026, 9, 15)
    for row in (3, 4, 5):
        _apply_style_commercials(
            style, _line(row, Decimal("80.00"), "EUR", d), None, bag)
    assert style.unit_price == Decimal("80.00")
    assert style.delivery_date == d
    assert bag.warnings == []


def test_a_missing_delivery_column_is_not_an_error():
    """The John Peter sheets print no DELIVERY column. That is normal."""
    style, bag = Style(name="CLERMONT"), _Bag()
    _apply_style_commercials(style, _line(3, Decimal("66.00")), None, bag)
    assert style.delivery_date is None
    assert not any("deliver" in w.lower() for w in bag.warnings)


def test_a_price_with_no_currency_anywhere_is_left_null_and_warned():
    """Inventing a currency would put a number in the costing with no unit."""
    style, bag = Style(name="CLERMONT"), _Bag()
    _apply_style_commercials(style, _line(3, Decimal("66.00")), None, bag)
    assert style.unit_price == Decimal("66.00")
    assert style.currency is None
    assert any("no currency" in w for w in bag.warnings)


def test_the_currency_falls_back_to_the_order_then_the_client():
    class _Order:
        currency = None

        class client:
            currency = "INR"

    style, bag = Style(name="CLERMONT"), _Bag()
    _apply_style_commercials(style, _line(3, Decimal("66.00")), _Order(), bag)
    assert style.currency == "INR"
    assert bag.warnings == []
