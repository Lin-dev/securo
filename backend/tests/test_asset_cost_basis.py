"""asset_service.total_cost_basis: what a held position cost, per source.

`purchase_price` is a total for manual, ledger-backed and Pluggy assets but a
per-share price for Plaid and SimpleFIN, and SimpleFIN reports an unknown
cost basis as "0.00". Reading any of those literally turns the whole position
value into "gain".
"""
import uuid
from decimal import Decimal

from app.models.asset import Asset
from app.services.asset_service import total_cost_basis


def _asset(*, source="manual", connected=False, purchase_price=None, units=None, metadata=None, average_price=None) -> Asset:
    return Asset(
        id=uuid.uuid4(), user_id=uuid.uuid4(), workspace_id=uuid.uuid4(), name="X", type="investment",
        currency="USD", source=source, connection_id=uuid.uuid4() if connected else None,
        purchase_price=Decimal(str(purchase_price)) if purchase_price is not None else None,
        units=Decimal(str(units)) if units is not None else None,
        average_price=Decimal(str(average_price)) if average_price is not None else None,
        external_metadata=metadata, valuation_method="manual",
    )


def test_manual_asset_purchase_price_is_the_total():
    assert total_cost_basis(_asset(purchase_price=900)) == 900.0


def test_ledger_backed_asset_purchase_price_is_the_total():
    asset = _asset(source="simplefin", connected=True, purchase_price=1234.5, units=10, average_price=123.45,
                   metadata={"cost_basis": "0.00"})
    assert total_cost_basis(asset) == 1234.5


def test_simplefin_zero_cost_basis_is_unknown_and_per_share_price_scales_by_units():
    # AAPL as SimpleFIN reports it: per-share purchase price, cost basis "0.00"
    asset = _asset(source="simplefin", connected=True, purchase_price=100.10, units=36.863747,
                   metadata={"symbol": "AAPL", "cost_basis": "0.00"})
    assert total_cost_basis(asset) == 3690.06


def test_simplefin_reported_cost_basis_wins_when_positive():
    asset = _asset(source="simplefin", connected=True, purchase_price=100.10, units=36.863747,
                   metadata={"cost_basis": "3500.25"})
    assert total_cost_basis(asset) == 3500.25


def test_simplefin_without_a_usable_price_or_units_is_unknown():
    assert total_cost_basis(_asset(source="simplefin", connected=True, purchase_price=0, units=5,
                                   metadata={"cost_basis": "0.00"})) is None
    assert total_cost_basis(_asset(source="simplefin", connected=True, purchase_price=10, units=None,
                                   metadata={"cost_basis": None})) is None


def test_plaid_uses_reported_cost_basis_then_price_times_units():
    assert total_cost_basis(_asset(source="plaid", connected=True, purchase_price=15, units=100,
                                   metadata={"cost_basis": "1500"})) == 1500.0
    assert total_cost_basis(_asset(source="plaid", connected=True, purchase_price=10, units=5, metadata={})) == 50.0


def test_pluggy_purchase_price_is_the_total():
    assert total_cost_basis(_asset(source="pluggy", connected=True, purchase_price=2000, units=7)) == 2000.0


def test_unknown_connected_source_is_unknown():
    assert total_cost_basis(_asset(source="enable_banking", connected=True, purchase_price=10, units=5)) is None
