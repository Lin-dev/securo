"""Default buckets and overrides (fork, plan P3)."""
import pytest

from app.schemas.wealth import AccountAnnotation
from app.services.wealth.buckets import (
    AccountFacts,
    asset_bucket,
    choose_bucket,
    default_bucket,
    fallback_key,
)


@pytest.mark.parametrize("name, typ, balance, expected", [
    ("TD COMPLETE CHECKING (3154)", "checking", 1000, "cash"),
    ("Wallet", "wallet", 50, "cash"),
    ("CHASE SAVINGS (5923)", "savings", 1000, "cash"),
    ("American Express Gold Card (2009)", "credit_card", -500, "card_debt"),
    ("Platinum Card (4004)", "credit_card", 0, "card_debt"),
    ("Bloomberg L.P. 401(k) Plan (2-01)", "investment", 1000, "retirement_pretax"),
    ("MAIMONIDES MEDICAL CENTER TAX DEFERRED RETIREMENT PLAN (07I0)", "investment", 1000, "retirement_pretax"),
    ("Retirement - Tax-Coordinated Portfolio - Roth IRA (0067)", "investment", 1000, "retirement_roth"),
    ("Retirement - Tax-Coordinated Portfolio - Roth 401(k) | Econify (3112)", "investment", 1000, "retirement_roth"),
    ("Traditional IRA (9388)", "investment", 1000, "retirement_pretax"),
    ("Crypto (0990)", "investment", 1000, "crypto"),
    ("Robinhood individual (6343)", "investment", 1000, "taxable"),
    ("Health Savings Account", "savings", 1000, "retirement_hsa"),  # Plaid types an HSA as savings
    ("Fidelity HSA", "investment", 1000, "retirement_hsa"),
    ("Car loan", "loan", -9000, "other_debt"),
    ("Overdrawn checking", "checking", -20, "other_debt"),  # a negative balance is debt
])
def test_default_buckets_follow_the_plan_order(name, typ, balance, expected):
    assert default_bucket(AccountFacts(id="a", name=name, type=typ, balance=balance)) == expected


def test_assets_outside_accounts():
    assert asset_bucket("investment") == "taxable"
    assert asset_bucket("real_estate") == asset_bucket("vehicle") == "property_other"


def test_override_by_id_wins_and_keeps_liquidity_rules():
    acct = AccountFacts(id="a1", name="Robinhood individual", type="investment", balance=10)
    choice = choose_bucket(acct, {"a1": AccountAnnotation(bucket="retirement_roth")})
    assert (choice.bucket, choice.default_bucket, choice.source, choice.liquid) == ("retirement_roth", "taxable", "user", False)
    liquid_override = choose_bucket(acct, {"a1": AccountAnnotation(liquid=False)})
    assert (liquid_override.bucket, liquid_override.liquid, liquid_override.source) == ("taxable", False, "user")


def test_override_follows_a_reconnected_account_through_its_fallback_key():
    old = AccountFacts(id="old-id", name="Chase Freedom (3835)", type="credit_card", balance=-10, institution="Chase", masked_number="3835")
    new = AccountFacts(id="new-id", name="Chase Freedom (3835)", type="credit_card", balance=-10, institution="Chase", masked_number="3835")
    overrides = {"old-id": AccountAnnotation(apr=24.99, fallback_key=fallback_key(old))}
    choice = choose_bucket(new, overrides)
    assert (choice.apr, choice.override_key, choice.source) == (24.99, "old-id", "user")


def test_no_override_is_a_default_choice():
    acct = AccountFacts(id="a1", name="TD SIMPLE SAVINGS", type="savings", balance=5)
    choice = choose_bucket(acct, {})
    assert (choice.bucket, choice.liquid, choice.source, choice.override_key, choice.apr) == ("cash", True, "default", "a1", None)
