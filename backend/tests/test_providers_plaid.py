"""Plaid provider: link tokens, the exchange + first pull, cursor sync,
investments, holdings, error mapping. HTTP is faked with httpx.MockTransport
patched onto PlaidProvider._client, like the SimpleFIN tests."""

import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from app.agents.services.crypto import decrypt
from app.core.config import Settings
from app.providers import all_known_providers
from app.providers.base import (
    ProviderNotConfiguredError,
    ProviderRateLimited,
    ProviderUserActionRequired,
    SessionExpiredError,
)
from app.providers.plaid import (
    NOT_READY_ATTEMPTS,
    PlaidProvider,
    _map_account,
    _map_holding,
    _map_investment_transaction,
    _map_transaction,
    _raise_for_error,
)

TODAY = date.today()


def _settings(**over) -> Settings:
    base = dict(
        plaid_client_id="cid-123",
        plaid_secret=SecretStr("prod-secret"),
        plaid_sandbox_secret=SecretStr("sand-secret"),
        plaid_env="production",
        plaid_days_requested=730,
        frontend_url="https://finance.example.com",
    )
    base.update(over)
    return Settings(**base)


class FakePlaid:
    """Routes by path; each route holds a queue of responses (last one repeats)."""

    def __init__(self):
        self.calls: list[tuple[str, dict, str]] = []
        self.routes: dict[str, list] = {}

    def on(self, path, *responses):
        self.routes[path] = list(responses)
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        path = request.url.path
        self.calls.append((path, body, request.url.host))
        queue = self.routes.get(path)
        if not queue:
            return httpx.Response(404, json={"error_type": "TEST", "error_code": "NO_ROUTE", "error_message": path})
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(response):
            response = response(body)
        if isinstance(response, tuple):
            status, payload = response
        else:
            status, payload = 200, response
        return httpx.Response(status, json=payload)

    def bodies(self, path):
        return [b for p, b, _ in self.calls if p == path]

    def hosts(self, path):
        return [h for p, _, h in self.calls if p == path]


def _err(code, status=400, error_type="ITEM_ERROR"):
    return (status, {"error_type": error_type, "error_code": code, "error_message": f"{code} happened"})


ACCOUNTS = {
    "accounts": [
        {"account_id": "acc-chk", "name": "Plaid Checking", "official_name": "Plaid Gold Checking", "mask": "0000",
         "type": "depository", "subtype": "checking", "balances": {"current": 110.5, "available": 100, "limit": None, "iso_currency_code": "USD"}},
        {"account_id": "acc-sav", "name": "Plaid Saving", "official_name": None, "mask": "1111",
         "type": "depository", "subtype": "savings", "balances": {"current": None, "available": 2500, "iso_currency_code": "USD"}},
        {"account_id": "acc-cc", "name": "Plaid Credit Card", "official_name": "Plaid Diamond Card", "mask": "3333",
         "type": "credit", "subtype": "credit card", "balances": {"current": 410, "limit": 2000, "iso_currency_code": "USD"}},
        {"account_id": "acc-inv", "name": "Plaid IRA", "official_name": None, "mask": "5555",
         "type": "investment", "subtype": "ira", "balances": {"current": 415.57, "iso_currency_code": "USD"}},
        {"account_id": "acc-loan", "name": "Plaid Student Loan", "mask": "7777",
         "type": "loan", "subtype": "student", "balances": {"current": 65262, "iso_currency_code": "USD"}},
    ],
    "item": {"item_id": "item-1"},
}

SYNC_PAGE_1 = {
    "added": [
        {"transaction_id": "t-coffee", "account_id": "acc-chk", "amount": 4.5, "date": str(TODAY - timedelta(days=3)),
         "name": "SQ *BLUE BOTTLE", "merchant_name": "Blue Bottle", "original_description": "SQ *BLUE BOTTLE 123",
         "pending": False, "pending_transaction_id": "t-coffee-pending", "iso_currency_code": "USD",
         "personal_finance_category": {"primary": "FOOD_AND_DRINK"}},
        {"transaction_id": "t-pay", "account_id": "acc-chk", "amount": -2500, "date": str(TODAY - timedelta(days=2)),
         "name": "ACME PAYROLL", "merchant_name": None, "pending": False, "iso_currency_code": "USD"},
        {"transaction_id": "t-inv-cash", "account_id": "acc-inv", "amount": 100, "date": str(TODAY - timedelta(days=2)),
         "name": "CASH SWEEP", "pending": False, "iso_currency_code": "USD"},
    ],
    "modified": [],
    "removed": [],
    "next_cursor": "cursor-1",
    "has_more": True,
}
SYNC_PAGE_2 = {
    "added": [
        {"transaction_id": "t-hold", "account_id": "acc-cc", "amount": 30, "date": str(TODAY - timedelta(days=1)),
         "name": "HOTEL HOLD", "pending": True, "iso_currency_code": "USD"},
    ],
    "modified": [
        {"transaction_id": "t-pay", "account_id": "acc-chk", "amount": -2500, "date": str(TODAY - timedelta(days=2)),
         "name": "ACME PAYROLL DIRECT DEP", "pending": False, "iso_currency_code": "USD"},
    ],
    "removed": [{"transaction_id": "t-coffee-pending", "account_id": "acc-chk"}],
    "next_cursor": "cursor-2",
    "has_more": False,
}
INV_TX = {
    "investment_transactions": [
        {"investment_transaction_id": "it-buy", "account_id": "acc-inv", "amount": 250, "date": str(TODAY - timedelta(days=5)),
         "name": "BUY VTI", "type": "buy", "subtype": "buy", "quantity": 1, "price": 250, "fees": 0, "security_id": "sec-vti", "iso_currency_code": "USD"},
        {"investment_transaction_id": "it-div", "account_id": "acc-inv", "amount": -8.72, "date": str(TODAY - timedelta(days=4)),
         "name": "INCOME DIV DIVIDEND RECEIVED", "type": "cash", "subtype": "dividend", "security_id": "sec-vti", "iso_currency_code": "USD"},
        {"investment_transaction_id": "it-cancel", "account_id": "acc-inv", "amount": 5, "date": str(TODAY),
         "name": "CANCEL", "type": "cancel", "subtype": "cancel"},
        {"investment_transaction_id": "it-zero", "account_id": "acc-inv", "amount": 0, "date": str(TODAY),
         "name": "TRANSFER IN KIND", "type": "transfer", "subtype": "transfer"},
    ],
    "total_investment_transactions": 4,
}
HOLDINGS = {
    "accounts": [{"account_id": "acc-inv", "name": "Plaid IRA", "official_name": None}],
    "holdings": [
        {"account_id": "acc-inv", "security_id": "sec-vti", "quantity": 2, "institution_price": 250.5, "institution_value": 501,
         "cost_basis": 480, "iso_currency_code": "USD", "institution_price_as_of": str(TODAY)},
        {"account_id": "acc-inv", "security_id": "sec-cash", "quantity": 12.34, "institution_price": 1, "institution_value": 12.34,
         "cost_basis": None, "iso_currency_code": "USD"},
    ],
    "securities": [
        {"security_id": "sec-vti", "ticker_symbol": "vti", "name": "Vanguard Total Stock Market ETF", "type": "etf",
         "isin": "US9229087690", "is_cash_equivalent": False, "iso_currency_code": "USD"},
        {"security_id": "sec-cash", "ticker_symbol": None, "name": "U S Dollar", "type": "cash", "is_cash_equivalent": True},
    ],
}
EXCHANGE = {"access_token": "access-sandbox-abc", "item_id": "item-1"}
ITEM = {"item": {"item_id": "item-1", "institution_id": "ins_109508"}}
INSTITUTION = {"institution": {"institution_id": "ins_109508", "name": "First Platypus Bank", "url": "https://www.firstplatypus.example"}}
LINK_TOKEN = {"link_token": "link-sandbox-xyz", "expiration": "2030-01-01T00:00:00Z"}


def _full_fake() -> FakePlaid:
    return (
        FakePlaid()
        .on("/link/token/create", LINK_TOKEN)
        .on("/item/public_token/exchange", EXCHANGE)
        .on("/item/get", ITEM)
        .on("/institutions/get_by_id", INSTITUTION)
        .on("/accounts/get", ACCOUNTS)
        .on("/transactions/sync", SYNC_PAGE_1, SYNC_PAGE_2)
        .on("/investments/transactions/get", INV_TX)
        .on("/investments/holdings/get", HOLDINGS)
        .on("/item/remove", {"removed": True})
    )


@pytest.fixture
def fake():
    server = _full_fake()
    transport = httpx.MockTransport(server.handler)

    async def fake_client(self):  # noqa: ANN001
        return httpx.AsyncClient(transport=transport, timeout=30)

    with patch.object(PlaidProvider, "_client", fake_client), \
         patch("app.providers.plaid.get_settings", return_value=_settings()), \
         patch("app.providers.plaid.asyncio.sleep", return_value=None):
        yield server


def _creds(provider: PlaidProvider, **over):
    creds = provider._store_token({"item_id": "item-1", "env": "production", "cursor": None, "inv_synced_through": None}, "access-1")
    creds.update(over)
    return creds


# --------------------------------------------------------------------------- #
# link tokens
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_create_connect_token_asks_for_two_years_and_the_registered_redirect(fake):
    token = await PlaidProvider().create_connect_token("user-42")
    assert token.access_token == "link-sandbox-xyz"
    (body,) = fake.bodies("/link/token/create")
    assert body["client_id"] == "cid-123" and body["secret"] == "prod-secret"
    assert body["products"] == ["transactions"]
    assert body["optional_products"] == ["investments"]
    assert body["transactions"] == {"days_requested": 730}
    assert body["redirect_uri"] == "https://finance.example.com/plaid/oauth"
    assert body["user"] == {"client_user_id": "user-42"}
    assert body["client_name"] == "Securo" and body["country_codes"] == ["US"] and body["language"] == "en"
    assert fake.hosts("/link/token/create") == ["production.plaid.com"]
    assert "access_token" not in body


@pytest.mark.asyncio
async def test_reconnect_token_is_update_mode_on_the_items_own_environment(fake):
    provider = PlaidProvider()
    creds = _creds(provider, env="sandbox")  # linked in sandbox while settings say production
    token = await provider.create_reconnect_token("user-42", creds)
    assert token.access_token == "link-sandbox-xyz"
    (body,) = fake.bodies("/link/token/create")
    assert body["access_token"] == "access-1" and body["secret"] == "sand-secret"
    assert "products" not in body and "transactions" not in body
    assert body["redirect_uri"] == "https://finance.example.com/plaid/oauth"
    assert fake.hosts("/link/token/create") == ["sandbox.plaid.com"]


def test_redirect_uri_default_and_override():
    with patch("app.providers.plaid.get_settings", return_value=_settings()):
        assert PlaidProvider().redirect_uri == "https://finance.example.com/plaid/oauth"
    with patch("app.providers.plaid.get_settings", return_value=_settings(plaid_oauth_redirect_uri="https://x.example/back")):
        assert PlaidProvider().redirect_uri == "https://x.example/back"


def test_registry_lists_plaid_with_the_link_flow():
    entry = next(p for p in all_known_providers() if p["name"] == "plaid")
    assert entry["flow_type"] == "link"
    assert entry["supports_asset_sync"] is True
    assert entry["requires_institution_select"] is False
    assert PlaidProvider().flow_type == "link"
    assert PlaidProvider().name == "plaid"


# --------------------------------------------------------------------------- #
# link completion + first pull
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_handle_oauth_callback_links_the_item_and_pulls_its_first_data(fake):
    provider = PlaidProvider()
    data = await provider.handle_oauth_callback("public-sandbox-token")

    assert data.external_id == "item-1"
    assert data.institution_name == "First Platypus Bank"
    assert data.logo_url and "firstplatypus.example" in data.logo_url
    assert decrypt(data.credentials["access_token_enc"]) == "access-sandbox-abc"
    assert "access_token" not in data.credentials
    assert data.credentials["item_id"] == "item-1"
    assert data.credentials["env"] == "production"
    assert data.credentials["cursor"] == "cursor-2"
    assert data.credentials["inv_synced_through"] == TODAY.isoformat()
    assert data.credentials["institution_id"] == "ins_109508"

    # loan skipped; the rest mapped with the institution stamped on each
    assert [a.external_id for a in data.accounts] == ["acc-chk", "acc-sav", "acc-cc", "acc-inv"]
    assert all(a.institution_name == "First Platypus Bank" and a.institution_external_id == "ins_109508" for a in data.accounts)

    # the import path reads transactions from the same instance without a second sync
    sync_calls_before = len(fake.bodies("/transactions/sync"))
    chk = await provider.get_transactions(data.credentials, "acc-chk", None)
    assert sorted(t.external_id for t in chk) == ["t-coffee", "t-pay", "t-pay"]  # added + modified both surface
    coffee = next(t for t in chk if t.external_id == "t-coffee")
    assert coffee.pending_external_id == "t-coffee-pending"
    inv = await provider.get_transactions(data.credentials, "acc-inv", None)
    assert sorted(t.external_id for t in inv) == ["it-buy", "it-div"]  # sync row for the investment account dropped
    assert await provider.get_removed_transaction_ids(data.credentials, "acc-chk") == ["t-coffee-pending"]
    assert await provider.get_removed_transaction_ids(data.credentials, "acc-cc") == []
    assert len(fake.bodies("/transactions/sync")) == sync_calls_before == 2
    assert fake.bodies("/transactions/sync")[0].get("cursor") is None
    assert fake.bodies("/transactions/sync")[1]["cursor"] == "cursor-1"
    assert fake.bodies("/transactions/sync")[0]["options"] == {"include_original_description": True}
    (inv_body,) = fake.bodies("/investments/transactions/get")
    assert inv_body["start_date"] == (TODAY - timedelta(days=730)).isoformat()
    assert inv_body["end_date"] == TODAY.isoformat()


@pytest.mark.asyncio
async def test_not_ready_item_keeps_no_cursor_and_returns_its_accounts(fake):
    fake.on("/transactions/sync", {"added": [], "modified": [], "removed": [], "next_cursor": "", "has_more": False,
                                   "transactions_update_status": "NOT_READY"})
    provider = PlaidProvider()
    data = await provider.handle_oauth_callback("public-token")
    assert data.credentials["cursor"] is None
    assert [a.external_id for a in data.accounts] == ["acc-chk", "acc-sav", "acc-cc", "acc-inv"]
    assert len(fake.bodies("/transactions/sync")) == NOT_READY_ATTEMPTS
    assert await provider.get_transactions(data.credentials, "acc-chk", None) == []


@pytest.mark.asyncio
async def test_institution_lookup_failure_does_not_block_linking(fake):
    fake.on("/institutions/get_by_id", _err("INTERNAL_SERVER_ERROR", 500, "API_ERROR"))
    data = await PlaidProvider().handle_oauth_callback("public-token")
    assert data.external_id == "item-1"
    assert data.institution_name == "Plaid"
    assert data.logo_url is None


# --------------------------------------------------------------------------- #
# reads on an existing item
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_refresh_credentials_advances_the_cursor_and_the_loader_runs_once(fake):
    provider = PlaidProvider()
    creds = _creds(provider, cursor="cursor-0", inv_synced_through=(TODAY - timedelta(days=30)).isoformat())
    refreshed = await provider.refresh_credentials(creds)
    assert refreshed["cursor"] == "cursor-2"
    assert refreshed["inv_synced_through"] == TODAY.isoformat()
    assert refreshed["access_token_enc"] == creds["access_token_enc"]

    accounts = await provider.get_accounts(creds)
    assert [a.type for a in accounts] == ["checking", "savings", "credit_card", "investment"]
    await provider.get_transactions(creds, "acc-chk", None)
    await provider.get_removed_transaction_ids(creds, "acc-chk")
    assert len(fake.bodies("/accounts/get")) == 1
    assert len(fake.bodies("/transactions/sync")) == 2
    assert fake.bodies("/transactions/sync")[0]["cursor"] == "cursor-0"
    # watermark rewinds two weeks
    (inv_body,) = fake.bodies("/investments/transactions/get")
    assert inv_body["start_date"] == (TODAY - timedelta(days=44)).isoformat()


@pytest.mark.asyncio
async def test_mutation_during_pagination_restarts_from_the_stored_cursor(fake):
    fake.on("/transactions/sync", SYNC_PAGE_1, _err("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", 400, "TRANSACTIONS_ERROR"),
            SYNC_PAGE_1, SYNC_PAGE_2)
    provider = PlaidProvider()
    creds = _creds(provider, cursor="cursor-0")
    refreshed = await provider.refresh_credentials(creds)
    assert refreshed["cursor"] == "cursor-2"
    assert [b.get("cursor") for b in fake.bodies("/transactions/sync")] == ["cursor-0", "cursor-1", "cursor-0", "cursor-1"]
    rows = await provider.get_transactions(creds, "acc-chk", None)
    assert sorted(t.external_id for t in rows) == ["t-coffee", "t-pay", "t-pay"]  # no double counting of the aborted pass


@pytest.mark.asyncio
async def test_investments_not_ready_on_a_live_sync_is_transient_but_tolerated_at_link_time(fake):
    fake.on("/investments/transactions/get", _err("PRODUCT_NOT_READY", 400, "ITEM_ERROR"))
    provider = PlaidProvider()
    with pytest.raises(ProviderRateLimited):
        await provider.refresh_credentials(_creds(provider))
    linked = await PlaidProvider().handle_oauth_callback("public-token")
    assert linked.credentials["inv_synced_through"] is None
    assert linked.credentials["cursor"] == "cursor-2"


@pytest.mark.asyncio
async def test_investments_pagination_and_no_investment_accounts(fake):
    page1 = {"investment_transactions": INV_TX["investment_transactions"][:2], "total_investment_transactions": 4}
    page2 = {"investment_transactions": INV_TX["investment_transactions"][2:], "total_investment_transactions": 4}
    fake.on("/investments/transactions/get", page1, page2)
    provider = PlaidProvider()
    rows = await provider.get_transactions(_creds(provider), "acc-inv", None)
    assert sorted(t.external_id for t in rows) == ["it-buy", "it-div"]
    assert [b["options"]["offset"] for b in fake.bodies("/investments/transactions/get")] == [0, 2]

    fake.on("/investments/transactions/get", _err("NO_INVESTMENT_ACCOUNTS", 400, "INVESTMENTS_ERROR"))
    other = PlaidProvider()
    assert await other.get_transactions(_creds(other), "acc-inv", None) == []


@pytest.mark.asyncio
async def test_payee_source_variants(fake):
    provider = PlaidProvider()
    creds = _creds(provider)
    auto = {t.external_id: t.payee for t in await provider.get_transactions(creds, "acc-chk", None, payee_source="auto")}
    assert auto["t-coffee"] == "Blue Bottle"
    by_desc = {t.external_id: t.payee for t in await provider.get_transactions(creds, "acc-chk", None, payee_source="description")}
    assert by_desc["t-coffee"] == "SQ *BLUE BOTTLE"
    none = await provider.get_transactions(creds, "acc-chk", None, payee_source="none")
    assert all(t.payee is None for t in none)


@pytest.mark.asyncio
async def test_holdings_map_securities_and_prices(fake):
    provider = PlaidProvider()
    holdings = await provider.get_holdings(_creds(provider))
    assert [h.external_id for h in holdings] == ["acc-inv:sec-vti", "acc-inv:sec-cash"]
    vti = holdings[0]
    assert vti.ticker == "VTI" and vti.name == "Vanguard Total Stock Market ETF"
    assert vti.current_value == Decimal("501") and vti.unit_price == Decimal("250.5") and vti.quantity == Decimal("2")
    assert vti.purchase_price == Decimal("240")
    assert vti.isin == "US9229087690"
    assert vti.account_external_id == "acc-inv" and vti.account_name == "Plaid IRA"
    assert vti.metadata["cost_basis"] == "480" and vti.metadata["security_type"] == "etf"
    cash = holdings[1]
    assert cash.ticker is None and cash.purchase_price is None and cash.metadata["is_cash_equivalent"] is True

    fake.on("/investments/holdings/get", _err("NO_INVESTMENT_ACCOUNTS", 400, "INVESTMENTS_ERROR"))
    assert await PlaidProvider().get_holdings(_creds(provider)) == []


@pytest.mark.asyncio
async def test_revoke_removes_the_item_with_the_decrypted_token(fake):
    provider = PlaidProvider()
    await provider.revoke(_creds(provider))
    (body,) = fake.bodies("/item/remove")
    assert body["access_token"] == "access-1"


@pytest.mark.asyncio
async def test_institution_logo_backfill(fake):
    provider = PlaidProvider()
    assert await provider.get_institution_logo(_creds(provider, institution_id="ins_109508")) is not None
    assert await provider.get_institution_logo(_creds(provider)) is None


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("code,status,exc", [
    ("ITEM_LOGIN_REQUIRED", 400, ProviderUserActionRequired),
    ("INVALID_CREDENTIALS", 400, ProviderUserActionRequired),
    ("ITEM_LOCKED", 400, ProviderUserActionRequired),
    ("USER_SETUP_REQUIRED", 400, ProviderUserActionRequired),
    ("INVALID_ACCESS_TOKEN", 400, SessionExpiredError),
    ("ITEM_NOT_FOUND", 400, SessionExpiredError),
    ("PRODUCT_NOT_READY", 400, ProviderRateLimited),
    ("RATE_LIMIT_EXCEEDED", 429, ProviderRateLimited),
    ("INSTITUTION_DOWN", 400, ProviderRateLimited),
    ("INVALID_API_KEYS", 400, ProviderNotConfiguredError),
    ("SOMETHING_NEW", 400, RuntimeError),
])
def test_error_mapping(code, status, exc):
    with pytest.raises(exc) as info:
        _raise_for_error({"error_type": "X", "error_code": code, "error_message": "m"}, status, "/x")
    assert code in str(info.value)
    if exc is ProviderUserActionRequired:
        assert info.value.code == "credentials_invalid" and info.value.help_url


@pytest.mark.asyncio
async def test_http_error_reaches_the_caller_mapped(fake):
    fake.on("/accounts/get", _err("ITEM_LOGIN_REQUIRED"))
    provider = PlaidProvider()
    with pytest.raises(ProviderUserActionRequired) as info:
        await provider.get_accounts(_creds(provider))
    assert info.value.code == "credentials_invalid"


@pytest.mark.asyncio
async def test_network_failure_is_transient_not_an_auth_error():
    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    async def fake_client(self):  # noqa: ANN001
        return httpx.AsyncClient(transport=httpx.MockTransport(boom), timeout=5)

    with patch.object(PlaidProvider, "_client", fake_client), \
         patch("app.providers.plaid.get_settings", return_value=_settings()):
        provider = PlaidProvider()
        with pytest.raises(ProviderRateLimited):
            await provider.get_accounts(_creds(provider))


@pytest.mark.asyncio
async def test_missing_secret_or_token_are_configuration_and_session_errors():
    with patch("app.providers.plaid.get_settings", return_value=_settings(plaid_secret=SecretStr(""))):
        with pytest.raises(ProviderNotConfiguredError):
            await PlaidProvider().create_connect_token("u")
    with patch("app.providers.plaid.get_settings", return_value=_settings()):
        with pytest.raises(SessionExpiredError):
            await PlaidProvider().refresh_credentials({"item_id": "i", "env": "production"})


# --------------------------------------------------------------------------- #
# pure mappers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ptype,subtype,expected", [
    ("depository", "checking", "checking"),
    ("depository", "savings", "savings"),
    ("depository", "money market", "savings"),
    ("depository", "cd", "savings"),
    ("depository", "hsa", "savings"),
    ("depository", "paypal", "checking"),
    ("credit", "credit card", "credit_card"),
    ("investment", "401k", "investment"),
    ("brokerage", "brokerage", "investment"),
    ("loan", "mortgage", None),
    ("other", "other", None),
])
def test_map_account_types(ptype, subtype, expected):
    mapped = _map_account({"account_id": "a", "name": "N", "mask": "1234", "type": ptype, "subtype": subtype,
                           "balances": {"current": 10, "iso_currency_code": "USD"}})
    assert (mapped.type if mapped else None) == expected


def test_map_account_details():
    chk = _map_account(ACCOUNTS["accounts"][0])
    assert chk.name == "Plaid Gold Checking (0000)" and chk.masked_number == "0000"
    assert chk.balance == Decimal("110.5") and chk.currency == "USD" and chk.credit_limit is None
    sav = _map_account(ACCOUNTS["accounts"][1])
    assert sav.balance == Decimal("2500")  # current missing → available
    cc = _map_account(ACCOUNTS["accounts"][2])
    assert cc.type == "credit_card" and cc.balance == Decimal("410") and cc.credit_limit == Decimal("2000")
    weird = _map_account({"account_id": "w", "name": "W", "type": "depository", "subtype": "checking",
                          "balances": {"current": 1, "iso_currency_code": "us dollars"}})
    assert weird.currency == "USD"


def test_map_transaction_details():
    raw = SYNC_PAGE_1["added"][0]
    t = _map_transaction(raw)
    assert t.type == "debit" and t.amount == Decimal("4.5")
    assert t.description == "SQ *BLUE BOTTLE"            # bank wording, so rules keep matching
    assert t.payee == "Blue Bottle" and t.status == "posted"
    assert t.pending_external_id == "t-coffee-pending"
    assert t.pluggy_category == "Food And Drink" and t.raw_data is raw and t.currency == "USD"
    credit = _map_transaction(SYNC_PAGE_1["added"][1])
    assert credit.type == "credit" and credit.amount == Decimal("2500") and credit.payee == "ACME PAYROLL"
    pending = _map_transaction(SYNC_PAGE_2["added"][0])
    assert pending.status == "pending" and pending.pending_external_id is None
    assert _map_transaction({"transaction_id": "x", "amount": "nope", "date": "2026-01-01"}) is None
    assert _map_transaction({"transaction_id": "x", "amount": 1, "date": None, "authorized_date": "2026-01-02"}).date == date(2026, 1, 2)


def test_map_investment_transaction_details():
    rows = INV_TX["investment_transactions"]
    buy = _map_investment_transaction(rows[0])
    assert buy.type == "debit" and buy.amount == Decimal("250") and buy.status == "posted" and buy.description == "BUY VTI"
    div = _map_investment_transaction(rows[1])
    assert div.type == "credit" and div.amount == Decimal("8.72")
    assert _map_investment_transaction(rows[2]) is None   # cancel
    assert _map_investment_transaction(rows[3]) is None   # zero amount


def test_map_holding_without_price_uses_value_over_quantity():
    h = _map_holding({"account_id": "a", "security_id": "s", "quantity": 4, "institution_value": 100},
                     {"s": {"name": "Fund"}}, {"a": "Acct"})
    assert h.unit_price == Decimal("25") and h.purchase_price is None and h.currency == "USD" and h.name == "Fund"
    assert _map_holding({"account_id": "a", "security_id": "s"}, {}, {}) is None
