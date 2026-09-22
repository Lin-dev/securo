"""Plaid provider (fork addition).

Plaid (https://plaid.com) links a bank login as an *Item*. Securo opens Plaid
Link in the browser with a short-lived ``link_token`` minted here; Link hands
back a ``public_token`` that :meth:`PlaidProvider.handle_oauth_callback`
exchanges for the Item's long-lived ``access_token``. From then on every read
is a JSON POST with ``client_id``/``secret`` in the body.

Shapes specific to Plaid live in this module:

* **Environments.** ``sandbox`` and ``production`` have different hosts and
  secrets. ``PLAID_ENV`` picks the environment for *new* links; each Item
  remembers its own ``env`` in the connection credentials, so a sandbox Item
  keeps working after the setting flips.
* **Cursor sync.** ``/transactions/sync`` is cursor based, not date based. The
  loader pulls every page once per provider instance and caches the result
  per Item; :meth:`refresh_credentials` returns the credentials with the new
  cursor, and the sync layer persists that dict only when the whole run
  commits, so a failed run never advances the cursor. ``since`` is ignored.
* **Settled ids.** Plaid mints a new ``transaction_id`` when a pending charge
  posts and lists the old one under ``removed``. Rows carry
  ``pending_external_id`` and the provider answers
  :meth:`get_removed_transaction_ids` so the sync layer can re-key and prune.
* **Investments.** Investment accounts get ``/investments/transactions/get``
  (date windowed by a watermark stored next to the cursor) and holdings from
  ``/investments/holdings/get``; their ``/transactions/sync`` rows are dropped
  so cash legs are not counted twice.
* **Skipped accounts.** ``loan`` and ``other`` accounts have no counterpart
  in Securo's account types yet and are left out with a log line.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx

from app.agents.services.crypto import decrypt, encrypt
from app.core.config import get_settings
from app.providers.base import (
    AccountData,
    BankProvider,
    ConnectionData,
    ConnectTokenData,
    HoldingData,
    ProviderNotConfiguredError,
    ProviderRateLimited,
    ProviderUserActionRequired,
    SessionExpiredError,
    TransactionData,
    mask_last4,
)
from app.providers.favicon import favicon_url_for

logger = logging.getLogger(__name__)

PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}
PLAID_API_VERSION = "2020-09-14"
PLAID_HTTP_TIMEOUT = 60.0
PLAID_HELP_URL = "https://my.plaid.com/"
SYNC_PAGE_SIZE = 500
INVESTMENTS_PAGE_SIZE = 500
# Re-read a little history on every investments pull, like the sync layer's
# own 14-day rewind, so a late-posted dividend is not missed.
INVESTMENTS_REWIND_DAYS = 14
# A freshly linked Item needs a moment before its first transactions are
# ready; the link path waits this long before giving up for the day.
NOT_READY_ATTEMPTS = 5
NOT_READY_DELAY_SECONDS = 1.5
MUTATION_RETRIES = 3

# Plaid error codes → what the sync layer should do about them.
_REAUTH_CODES = frozenset({
    "ITEM_LOGIN_REQUIRED", "INVALID_CREDENTIALS", "INVALID_MFA", "ITEM_LOCKED",
    "USER_SETUP_REQUIRED", "MFA_NOT_SUPPORTED", "INSUFFICIENT_CREDENTIALS",
    "USER_PERMISSION_REVOKED", "ACCESS_NOT_GRANTED", "ITEM_NO_VERIFICATION",
})
_EXPIRED_CODES = frozenset({"INVALID_ACCESS_TOKEN", "ITEM_NOT_FOUND"})
_TRANSIENT_CODES = frozenset({
    "PRODUCT_NOT_READY", "RATE_LIMIT_EXCEEDED", "ADDITION_LIMIT", "ITEM_GET_LIMIT",
    "TRANSACTIONS_LIMIT", "INSTITUTION_DOWN", "INSTITUTION_NOT_RESPONDING",
    "INSTITUTION_NOT_AVAILABLE", "INTERNAL_SERVER_ERROR", "PLANNED_MAINTENANCE",
})
_CONFIG_CODES = frozenset({"INVALID_API_KEYS", "UNAUTHORIZED_ENVIRONMENT", "INVALID_PRODUCT"})
# Investments endpoints answering "this Item has nothing of the kind".
_EMPTY_INVESTMENTS_CODES = frozenset({
    "NO_INVESTMENT_ACCOUNTS", "NO_INVESTMENT_AUTH_ACCOUNTS",
    "PRODUCTS_NOT_SUPPORTED", "PRODUCT_NOT_ENABLED",
})
_MUTATION_CODE = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"

_SKIPPED_ACCOUNT_TYPES = frozenset({"loan", "other"})
_SAVINGS_SUBTYPES = frozenset({
    "savings", "money market", "cd", "hsa", "cash management", "prepaid", "ebt",
})


class _PlaidEmpty(Exception):
    """An investments endpoint said the Item has no such data (not an error)."""


class _MutationDuringPagination(Exception):
    """Plaid changed the data under a paginated sync; restart from the cursor."""


@dataclass
class _ItemState:
    accounts: list[AccountData]
    account_types: dict[str, str]
    tx_by_account: dict[str, list[TransactionData]] = field(default_factory=dict)
    removed_by_account: dict[str, list[str]] = field(default_factory=dict)
    next_cursor: Optional[str] = None
    inv_through: Optional[str] = None
    not_ready: bool = False


# --------------------------------------------------------------------------- #
# Pure mappers (module level so they can be unit-tested without HTTP)
# --------------------------------------------------------------------------- #


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _iso_currency(value: Any) -> Optional[str]:
    if not value:
        return None
    code = str(value).strip().upper()
    return code if len(code) == 3 and code.isalpha() else None


def _humanize_category(raw: dict) -> Optional[str]:
    pfc = raw.get("personal_finance_category") or {}
    primary = pfc.get("primary") if isinstance(pfc, dict) else None
    if not primary:
        return None
    return str(primary).replace("_", " ").title()


def _payee_for(raw: dict, payee_source: str) -> Optional[str]:
    if payee_source in ("none", "payment_data"):
        return None
    if payee_source == "description":
        value = raw.get("name")
    else:  # auto, merchant
        value = raw.get("merchant_name") or raw.get("name")
    value = (value or "").strip()
    return value[:255] or None


def _map_account(raw: dict) -> Optional[AccountData]:
    """Plaid account → AccountData, or None for account kinds Securo lacks."""
    account_id = raw.get("account_id")
    if not account_id:
        return None
    ptype = (raw.get("type") or "").lower()
    subtype = (raw.get("subtype") or "").lower()
    if ptype in _SKIPPED_ACCOUNT_TYPES:
        return None
    if ptype == "depository":
        stype = "savings" if subtype in _SAVINGS_SUBTYPES else "checking"
    elif ptype == "credit":
        stype = "credit_card"
    elif ptype in ("investment", "brokerage"):
        stype = "investment"
    else:
        return None
    balances = raw.get("balances") or {}
    balance = _to_decimal(balances.get("current"))
    if balance is None:
        balance = _to_decimal(balances.get("available")) or Decimal("0")
    currency = (
        _iso_currency(balances.get("iso_currency_code"))
        or _iso_currency(balances.get("unofficial_currency_code"))
        or "USD"
    )
    base_name = (raw.get("official_name") or raw.get("name") or "Account").strip()
    mask = (raw.get("mask") or "").strip()
    name = f"{base_name} ({mask})" if mask and mask not in base_name else base_name
    return AccountData(
        external_id=account_id,
        name=name[:255],
        type=stype,
        balance=balance,
        currency=currency,
        credit_limit=_to_decimal(balances.get("limit")) if stype == "credit_card" else None,
        masked_number=mask_last4(mask) if mask else None,
    )


def _map_transaction(raw: dict, payee_source: str = "auto") -> Optional[TransactionData]:
    txn_id = raw.get("transaction_id")
    amount_raw = _to_decimal(raw.get("amount"))
    txn_date = _parse_date(raw.get("date")) or _parse_date(raw.get("authorized_date"))
    if not txn_id or amount_raw is None or txn_date is None:
        return None
    # Plaid: positive amounts are money leaving the account.
    txn_type = "debit" if amount_raw > 0 else "credit"
    # The bank's own wording goes in `description` so the user's rules (written
    # against bank strings) keep matching; Plaid's merchant name becomes the payee.
    description = (
        raw.get("name") or raw.get("original_description") or raw.get("merchant_name") or "Transaction"
    ).strip()[:500]
    return TransactionData(
        external_id=str(txn_id),
        description=description,
        amount=amount_raw.copy_abs(),
        date=txn_date,
        type=txn_type,
        currency=_iso_currency(raw.get("iso_currency_code")) or _iso_currency(raw.get("unofficial_currency_code")),
        pluggy_category=_humanize_category(raw),
        status="pending" if raw.get("pending") else "posted",
        payee=_payee_for(raw, payee_source),
        raw_data=raw,
        pending_external_id=(raw.get("pending_transaction_id") or None),
    )


def _map_investment_transaction(raw: dict) -> Optional[TransactionData]:
    txn_id = raw.get("investment_transaction_id")
    amount_raw = _to_decimal(raw.get("amount"))
    txn_date = _parse_date(raw.get("date"))
    if not txn_id or amount_raw is None or txn_date is None:
        return None
    if (raw.get("type") or "").lower() == "cancel" or amount_raw == 0:
        return None
    txn_type = "debit" if amount_raw > 0 else "credit"
    description = (
        raw.get("name") or f"{raw.get('type') or ''} {raw.get('subtype') or ''}".strip() or "Investment transaction"
    ).strip()[:500]
    return TransactionData(
        external_id=str(txn_id),
        description=description,
        amount=amount_raw.copy_abs(),
        date=txn_date,
        type=txn_type,
        currency=_iso_currency(raw.get("iso_currency_code")) or _iso_currency(raw.get("unofficial_currency_code")),
        status="posted",
        payee=None,
        raw_data=raw,
    )


def _map_holding(raw: dict, securities: dict[str, dict], account_names: dict[str, str]) -> Optional[HoldingData]:
    account_id = raw.get("account_id")
    security_id = raw.get("security_id")
    value = _to_decimal(raw.get("institution_value"))
    if not account_id or not security_id or value is None:
        return None
    security = securities.get(security_id, {})
    quantity = _to_decimal(raw.get("quantity"))
    price = _to_decimal(raw.get("institution_price"))
    if price is None and quantity:
        price = value / quantity
    cost = _to_decimal(raw.get("cost_basis"))
    purchase_price = (cost / quantity) if (cost is not None and quantity) else None
    ticker = (security.get("ticker_symbol") or "").strip().upper()[:32] or None
    name = (security.get("name") or ticker or security_id).strip()[:255]
    return HoldingData(
        external_id=f"{account_id}:{security_id}",
        name=name,
        currency=(
            _iso_currency(raw.get("iso_currency_code"))
            or _iso_currency(security.get("iso_currency_code"))
            or "USD"
        ),
        current_value=value,
        quantity=quantity,
        unit_price=price,
        purchase_price=purchase_price,
        isin=security.get("isin"),
        ticker=ticker,
        metadata={
            "security_id": security_id,
            "security_type": security.get("type"),
            "security_subtype": security.get("subtype"),
            "is_cash_equivalent": security.get("is_cash_equivalent"),
            "cost_basis": str(cost) if cost is not None else None,
            "institution_price_as_of": raw.get("institution_price_as_of"),
        },
        account_external_id=account_id,
        account_name=account_names.get(account_id),
    )


def _raise_for_error(data: Any, status_code: int, path: str) -> None:
    payload = data if isinstance(data, dict) else {}
    code = str(payload.get("error_code") or f"HTTP_{status_code}")
    message = str(payload.get("error_message") or payload.get("display_message") or "").strip()
    detail = f"Plaid {path}: {code}" + (f" ({message})" if message else "")
    if code == _MUTATION_CODE:
        raise _MutationDuringPagination(detail)
    if code in _EMPTY_INVESTMENTS_CODES:
        raise _PlaidEmpty(detail)
    if code in _REAUTH_CODES:
        raise ProviderUserActionRequired(detail, code="credentials_invalid", help_url=PLAID_HELP_URL)
    if code in _EXPIRED_CODES:
        raise SessionExpiredError(detail)
    if code in _TRANSIENT_CODES or status_code in (429, 502, 503, 504):
        raise ProviderRateLimited(detail)
    if code in _CONFIG_CODES:
        raise ProviderNotConfiguredError(detail)
    raise RuntimeError(detail)


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #


class PlaidProvider(BankProvider):
    def __init__(self, env: Optional[str] = None) -> None:
        # `env` overrides PLAID_ENV for new links (used by smoke tests that
        # exercise the sandbox from a production deployment).
        self._env_override = env
        self._items: dict[str, _ItemState] = {}

    # -- identity ---------------------------------------------------------- #

    @property
    def name(self) -> str:
        return "plaid"

    @property
    def flow_type(self) -> str:
        return "link"

    @property
    def redirect_uri(self) -> str:
        settings = get_settings()
        return (
            settings.plaid_oauth_redirect_uri
            or f"{settings.frontend_url.rstrip('/')}/plaid/oauth"
        )

    # -- configuration ----------------------------------------------------- #

    def _default_env(self) -> str:
        return (self._env_override or get_settings().plaid_env or "production").strip().lower()

    @staticmethod
    def _host_for(env: str) -> str:
        host = PLAID_HOSTS.get(env)
        if not host:
            raise ProviderNotConfiguredError(f"Unknown Plaid environment {env!r}")
        return host

    @staticmethod
    def _secret_for(env: str) -> str:
        settings = get_settings()
        secret = settings.plaid_sandbox_secret if env == "sandbox" else settings.plaid_secret
        value = secret.get_secret_value() if secret else ""
        if not settings.plaid_client_id or not value:
            raise ProviderNotConfiguredError(f"Plaid {env} credentials are not configured")
        return value

    def _country_codes(self) -> list[str]:
        codes = [c.strip().upper() for c in get_settings().plaid_country_codes.split(",")]
        return [c for c in codes if c] or ["US"]

    @staticmethod
    def _credentials_env(credentials: Optional[dict]) -> Optional[str]:
        env = (credentials or {}).get("env")
        return str(env).strip().lower() if env else None

    def _env_of(self, credentials: Optional[dict]) -> str:
        return self._credentials_env(credentials) or self._default_env()

    @staticmethod
    def _access_token(credentials: Optional[dict]) -> str:
        creds = credentials or {}
        token: Optional[str] = None
        enc = creds.get("access_token_enc")
        if enc:
            token = decrypt(enc)
        token = token or creds.get("access_token")
        if not token:
            raise SessionExpiredError("Plaid access token is missing; link the account again")
        return str(token)

    @staticmethod
    def _store_token(credentials: dict, access_token: str) -> dict:
        enc = encrypt(access_token)
        out = dict(credentials)
        if enc:
            out["access_token_enc"] = enc
            out.pop("access_token", None)
        else:  # encryption unavailable: keep it readable rather than lose it
            out["access_token"] = access_token
        return out

    # -- HTTP -------------------------------------------------------------- #

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=PLAID_HTTP_TIMEOUT,
            headers={
                "Content-Type": "application/json",
                "Plaid-Version": PLAID_API_VERSION,
                "User-Agent": "Securo/0.1 (+https://usesecuro.com)",
            },
        )

    async def _post(self, env: str, path: str, body: dict) -> dict:
        settings = get_settings()
        payload = {"client_id": settings.plaid_client_id, "secret": self._secret_for(env), **body}
        url = f"{self._host_for(env)}{path}"
        try:
            async with await self._client() as client:
                resp = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            # A network blip is not the user's fault and not a credentials
            # problem: keep the connection active and try again next run.
            raise ProviderRateLimited(f"Plaid unreachable ({exc.__class__.__name__})") from exc
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("error_code")):
            _raise_for_error(data, resp.status_code, path)
        return data if isinstance(data, dict) else {}

    # -- link tokens ------------------------------------------------------- #

    def _link_body(self, client_user_id: str) -> dict:
        settings = get_settings()
        return {
            "client_name": (settings.plaid_client_name or "Securo")[:30],
            "language": "en",
            "country_codes": self._country_codes(),
            "user": {"client_user_id": str(client_user_id)},
            "redirect_uri": self.redirect_uri,
        }

    async def create_connect_token(self, client_user_id: str, item_id: str | None = None) -> ConnectTokenData:
        env = self._default_env()
        body = {
            **self._link_body(client_user_id),
            "products": ["transactions"],
            "optional_products": ["investments"],
            "transactions": {"days_requested": int(get_settings().plaid_days_requested)},
        }
        data = await self._post(env, "/link/token/create", body)
        return ConnectTokenData(access_token=data["link_token"])

    async def create_reconnect_token(self, client_user_id: str, credentials: dict) -> ConnectTokenData:
        """Update-mode link token: same Item, same access token, no products."""
        env = self._env_of(credentials)
        body = {**self._link_body(client_user_id), "access_token": self._access_token(credentials)}
        data = await self._post(env, "/link/token/create", body)
        return ConnectTokenData(access_token=data["link_token"])

    async def get_oauth_url(self, redirect_uri: str, state: str, flow_params: Optional[dict] = None) -> str:
        raise NotImplementedError("Plaid uses the Link popup flow, not an OAuth redirect")

    # -- link completion --------------------------------------------------- #

    async def handle_oauth_callback(self, code: str) -> ConnectionData:
        """Exchange a Link ``public_token`` and pull the Item's first data.

        Link-only: a reconnect (update mode) keeps the same access token and
        needs no exchange; the UI runs a plain sync afterwards instead.
        """
        env = self._default_env()
        exchanged = await self._post(env, "/item/public_token/exchange", {"public_token": code})
        access_token = exchanged["access_token"]
        item_id = exchanged["item_id"]

        item = (await self._post(env, "/item/get", {"access_token": access_token})).get("item") or {}
        institution_id = item.get("institution_id")
        institution_name, institution_url = "Plaid", None
        if institution_id:
            try:
                institution = await self._institution(env, institution_id)
                institution_name = institution.get("name") or institution_name
                institution_url = institution.get("url")
            except Exception:  # noqa: BLE001 - cosmetic; never fail a link over it
                logger.warning("Plaid institution lookup failed for %s", institution_id, exc_info=True)

        credentials = self._store_token(
            {"item_id": item_id, "env": env, "cursor": None, "inv_synced_through": None,
             "institution_id": institution_id},
            access_token,
        )
        state = await self._load_item(credentials, initial=True)
        logo_url = favicon_url_for(institution_url)
        accounts = [
            replace(
                acc,
                institution_external_id=institution_id,
                institution_name=institution_name,
                institution_logo_url=logo_url,
            )
            for acc in state.accounts
        ]
        return ConnectionData(
            external_id=item_id,
            institution_name=institution_name,
            credentials=self._credentials_with_state(credentials, state),
            accounts=accounts,
            logo_url=logo_url,
        )

    async def _institution(self, env: str, institution_id: str) -> dict:
        data = await self._post(
            env,
            "/institutions/get_by_id",
            {
                "institution_id": institution_id,
                "country_codes": self._country_codes(),
                "options": {"include_optional_metadata": True},
            },
        )
        return data.get("institution") or {}

    async def get_institution_logo(self, credentials: dict) -> Optional[str]:
        institution_id = (credentials or {}).get("institution_id")
        if not institution_id:
            return None
        try:
            institution = await self._institution(self._env_of(credentials), institution_id)
        except Exception:  # noqa: BLE001
            return None
        return favicon_url_for(institution.get("url"))

    # -- reads ------------------------------------------------------------- #

    @staticmethod
    def _credentials_with_state(credentials: dict, state: _ItemState) -> dict:
        out = dict(credentials)
        out["cursor"] = state.next_cursor
        out["inv_synced_through"] = state.inv_through
        return out

    async def refresh_credentials(self, credentials: dict) -> dict:
        """Pull the Item once and return the credentials carrying the new
        cursor and investments watermark (persisted by the sync layer)."""
        self._access_token(credentials)  # raises SessionExpiredError when gone
        state = await self._load_item(credentials)
        return self._credentials_with_state(credentials, state)

    async def get_accounts(self, credentials: dict) -> list[AccountData]:
        return list((await self._load_item(credentials)).accounts)

    async def get_transactions(
        self,
        credentials: dict,
        account_external_id: str,
        since: Optional[date] = None,
        payee_source: str = "auto",
    ) -> list[TransactionData]:
        # `since` is deliberately unused: the cursor decides what is new.
        state = await self._load_item(credentials)
        rows = state.tx_by_account.get(account_external_id, [])
        if payee_source == "auto":
            return list(rows)
        return [replace(row, payee=_payee_for(row.raw_data or {}, payee_source)) for row in rows]

    async def get_removed_transaction_ids(self, credentials: dict, account_external_id: str) -> list[str]:
        state = await self._load_item(credentials)
        return [
            *state.removed_by_account.get(account_external_id, []),
            *state.removed_by_account.get("", []),
        ]

    async def get_holdings(self, credentials: dict) -> list[HoldingData]:
        env, token = self._env_of(credentials), self._access_token(credentials)
        try:
            data = await self._post(env, "/investments/holdings/get", {"access_token": token})
        except _PlaidEmpty:
            return []
        securities = {s.get("security_id"): s for s in data.get("securities", []) if s.get("security_id")}
        names = {
            a.get("account_id"): (a.get("official_name") or a.get("name") or "")
            for a in data.get("accounts", [])
            if a.get("account_id")
        }
        holdings = [_map_holding(raw, securities, names) for raw in data.get("holdings", [])]
        return [h for h in holdings if h is not None]

    async def revoke(self, credentials: dict) -> None:
        env, token = self._env_of(credentials), self._access_token(credentials)
        await self._post(env, "/item/remove", {"access_token": token})

    # -- loader ------------------------------------------------------------ #

    async def _load_item(self, credentials: dict, *, initial: bool = False) -> _ItemState:
        item_id = str((credentials or {}).get("item_id") or "item")
        cached = self._items.get(item_id)
        if cached is not None:
            return cached
        env, token = self._env_of(credentials), self._access_token(credentials)

        raw_accounts = (await self._post(env, "/accounts/get", {"access_token": token})).get("accounts", [])
        accounts: list[AccountData] = []
        account_types: dict[str, str] = {}
        for raw in raw_accounts:
            mapped = _map_account(raw)
            if mapped is None:
                logger.info(
                    "Plaid account %s (%s/%s) skipped: no matching Securo account type",
                    raw.get("account_id"), raw.get("type"), raw.get("subtype"),
                )
                continue
            accounts.append(mapped)
            account_types[mapped.external_id] = mapped.type

        cursor = (credentials or {}).get("cursor") or None
        added, modified, removed, next_cursor, ready = await self._sync_pages(
            env, token, cursor, wait_for_ready=initial
        )
        state = _ItemState(accounts=accounts, account_types=account_types, next_cursor=next_cursor, not_ready=not ready)
        for raw in [*added, *modified]:
            account_id = raw.get("account_id") or ""
            if account_types.get(account_id) == "investment":
                continue  # served by the investments endpoint below
            mapped_tx = _map_transaction(raw)
            if mapped_tx is not None:
                state.tx_by_account.setdefault(account_id, []).append(mapped_tx)
        for entry in removed:
            txn_id = entry.get("transaction_id")
            if txn_id:
                state.removed_by_account.setdefault(entry.get("account_id") or "", []).append(str(txn_id))

        watermark = (credentials or {}).get("inv_synced_through") or None
        state.inv_through = watermark
        investment_accounts = [ext for ext, kind in account_types.items() if kind == "investment"]
        if investment_accounts:
            today = date.today()
            if watermark and _parse_date(watermark):
                start = _parse_date(watermark) - timedelta(days=INVESTMENTS_REWIND_DAYS)
            else:
                start = today - timedelta(days=int(get_settings().plaid_days_requested))
            try:
                for raw in await self._investment_transactions(env, token, start, today):
                    mapped_tx = _map_investment_transaction(raw)
                    if mapped_tx is not None:
                        state.tx_by_account.setdefault(raw.get("account_id") or "", []).append(mapped_tx)
                state.inv_through = today.isoformat()
            except ProviderRateLimited:
                if not initial:
                    raise
                # First pull right after linking: Plaid may still be preparing
                # investments. Leave the watermark empty; the next sync gets it.
                logger.info("Plaid investments not ready at link time for item %s", item_id)

        self._items[item_id] = state
        return state

    async def _sync_pages(
        self, env: str, token: str, cursor: Optional[str], *, wait_for_ready: bool
    ) -> tuple[list[dict], list[dict], list[dict], Optional[str], bool]:
        """All ``/transactions/sync`` pages from ``cursor``.

        Returns (added, modified, removed, next_cursor, ready). ``ready`` is
        False when Plaid has not finished the Item's first pull; the caller
        then keeps the old cursor.
        """
        attempts = NOT_READY_ATTEMPTS if wait_for_ready else 1
        for attempt in range(attempts):
            result = await self._sync_from(env, token, cursor)
            if result is not None:
                return (*result, True)
            if attempt + 1 < attempts:
                await asyncio.sleep(NOT_READY_DELAY_SECONDS)
        return [], [], [], cursor, False

    async def _sync_from(
        self, env: str, token: str, cursor: Optional[str]
    ) -> Optional[tuple[list[dict], list[dict], list[dict], Optional[str]]]:
        """One complete pass of pages, restarted from ``cursor`` if Plaid
        reports a mutation mid-pagination. None means NOT_READY."""
        for _ in range(MUTATION_RETRIES):
            added: list[dict] = []
            modified: list[dict] = []
            removed: list[dict] = []
            page_cursor = cursor
            try:
                while True:
                    body: dict[str, Any] = {
                        "access_token": token,
                        "count": SYNC_PAGE_SIZE,
                        "options": {"include_original_description": True},
                    }
                    if page_cursor:
                        body["cursor"] = page_cursor
                    data = await self._post(env, "/transactions/sync", body)
                    added.extend(data.get("added") or [])
                    modified.extend(data.get("modified") or [])
                    removed.extend(data.get("removed") or [])
                    next_cursor = data.get("next_cursor")
                    if data.get("has_more"):
                        page_cursor = next_cursor
                        continue
                    not_ready = (
                        next_cursor == "" or data.get("transactions_update_status") == "NOT_READY"
                    )
                    if not_ready and not added and not modified and not removed:
                        return None
                    return added, modified, removed, (next_cursor or cursor)
            except _MutationDuringPagination:
                logger.info("Plaid data changed during pagination; restarting from the stored cursor")
                continue
        raise ProviderRateLimited("Plaid transactions kept changing during pagination; retry later")

    async def _investment_transactions(self, env: str, token: str, start: date, end: date) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        while True:
            try:
                data = await self._post(
                    env,
                    "/investments/transactions/get",
                    {
                        "access_token": token,
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                        "options": {"count": INVESTMENTS_PAGE_SIZE, "offset": offset},
                    },
                )
            except _PlaidEmpty:
                return rows
            page = data.get("investment_transactions") or []
            rows.extend(page)
            offset += len(page)
            total = data.get("total_investment_transactions")
            if not page or (isinstance(total, int) and offset >= total):
                return rows
