"""Splitting one transaction into category lines (fork feature).

The parent is kept and hidden with is_ignored; its lines are ordinary rows,
so every total already counts them. These tests pin the contract the UI and
the rule path rely on: lines replace the parent one-for-one in the balance
and in the summary, a re-split replaces, an unsplit restores, and nothing
that would double count or strand a line is allowed through the API.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction

TODAY = date.today().isoformat()


async def _account(client: AsyncClient, auth_headers, name: str = "Checking") -> dict:
    resp = await client.post(
        "/api/accounts",
        headers=auth_headers,
        json={"name": name, "type": "checking", "balance": 0, "currency": "USD"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _category(client: AsyncClient, auth_headers, name: str, **extra) -> dict:
    resp = await client.post(
        "/api/categories", headers=auth_headers, json={"name": name, **extra}
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _transaction(
    client: AsyncClient, auth_headers, account_id: str, amount: str = "500.00", **extra
) -> dict:
    resp = await client.post(
        "/api/transactions",
        headers=auth_headers,
        json={
            "account_id": account_id,
            "description": "NORTHWESTERN MUTUAL",
            "amount": amount,
            "date": TODAY,
            "type": "debit",
            "currency": "USD",
            **extra,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _split(client: AsyncClient, auth_headers, tx_id: str, lines: list[dict]):
    return await client.post(
        f"/api/transactions/{tx_id}/split", headers=auth_headers, json={"lines": lines}
    )


async def _balance(client: AsyncClient, auth_headers, account_id: str) -> float:
    """The transaction-derived balance the accounts list shows."""
    resp = await client.get("/api/accounts", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    account = next(a for a in resp.json() if a["id"] == account_id)
    return float(account["current_balance"])


async def _rows_under(session: AsyncSession, parent_id: str) -> list[Transaction]:
    result = await session.execute(
        select(Transaction).where(Transaction.parent_transaction_id == uuid.UUID(parent_id))
    )
    return list(result.scalars().all())


async def _vul(client, auth_headers):
    """The motivating case: a 500 premium, half insurance, half invested."""
    account = await _account(client, auth_headers)
    insurance = await _category(client, auth_headers, "Insurance")
    invested = await _category(
        client, auth_headers, "Investment contribution", treat_as_transfer=True
    )
    tx = await _transaction(client, auth_headers, account["id"])
    lines = [
        {"category_id": insurance["id"], "amount": "250.00"},
        {"category_id": invested["id"], "amount": "250.00", "notes": "VUL cash value"},
    ]
    return account, insurance, invested, tx, lines


@pytest.mark.asyncio
async def test_split_creates_lines_and_hides_parent(client, auth_headers):
    account, insurance, invested, tx, lines = await _vul(client, auth_headers)

    resp = await _split(client, auth_headers, tx["id"], lines)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    parent = body["parent"]
    assert parent["id"] == tx["id"]
    assert parent["is_ignored"] is True
    assert parent["split_count"] == 2
    assert parent["parent_transaction_id"] is None
    children = body["children"]
    assert [float(c["amount"]) for c in children] == [250.0, 250.0]
    assert [c["category_id"] for c in children] == [insurance["id"], invested["id"]]
    assert children[1]["notes"] == "VUL cash value"
    for child in children:
        assert child["parent_transaction_id"] == tx["id"]
        assert child["source"] == "split"
        assert child["status"] == "posted"
        assert child["is_ignored"] is False
        assert child["split_count"] == 0
        assert child["account_id"] == account["id"]
        assert child["date"] == tx["date"]
        assert child["type"] == "debit"
        assert child["description"] == tx["description"]
        assert child["external_id"] is None

    detail = await client.get(f"/api/transactions/{tx['id']}", headers=auth_headers)
    assert detail.json()["split_count"] == 2
    listed = await client.get(
        f"/api/transactions?account_id={account['id']}", headers=auth_headers
    )
    by_id = {item["id"]: item for item in listed.json()["items"]}
    assert by_id[tx["id"]]["split_count"] == 2
    assert len(by_id) == 3


@pytest.mark.asyncio
async def test_split_lines_endpoint_lists_children_in_entry_order(client, auth_headers):
    _, insurance, invested, tx, lines = await _vul(client, auth_headers)
    lines[0]["amount"], lines[1]["amount"] = "100.00", "400.00"
    await _split(client, auth_headers, tx["id"], lines)

    resp = await client.get(f"/api/transactions/{tx['id']}/split", headers=auth_headers)

    assert resp.status_code == 200
    assert [(c["category_id"], float(c["amount"])) for c in resp.json()] == [
        (insurance["id"], 100.0),
        (invested["id"], 400.0),
    ]
    missing = await client.get(f"/api/transactions/{uuid.uuid4()}/split", headers=auth_headers)
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_split_keeps_account_balance(client, auth_headers):
    account, _, _, tx, lines = await _vul(client, auth_headers)
    before = await _balance(client, auth_headers, account["id"])
    assert before == pytest.approx(-500.0)

    await _split(client, auth_headers, tx["id"], lines)

    assert await _balance(client, auth_headers, account["id"]) == pytest.approx(before)


@pytest.mark.asyncio
async def test_resplit_replaces_lines(client, auth_headers, session: AsyncSession):
    _, insurance, invested, tx, lines = await _vul(client, auth_headers)
    first = await _split(client, auth_headers, tx["id"], lines)
    first_ids = {c["id"] for c in first.json()["children"]}

    lines[0]["amount"], lines[1]["amount"] = "300.00", "200.00"
    second = await _split(client, auth_headers, tx["id"], lines)

    assert second.status_code == 200, second.text
    children = second.json()["children"]
    assert [float(c["amount"]) for c in children] == [300.0, 200.0]
    assert {c["id"] for c in children}.isdisjoint(first_ids)
    rows = await _rows_under(session, tx["id"])
    assert len(rows) == 2
    assert second.json()["parent"]["split_count"] == 2


@pytest.mark.asyncio
async def test_unsplit_restores_parent(client, auth_headers, session: AsyncSession):
    account, _, _, tx, lines = await _vul(client, auth_headers)
    await _split(client, auth_headers, tx["id"], lines)

    resp = await client.delete(f"/api/transactions/{tx['id']}/split", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["is_ignored"] is False
    assert resp.json()["split_count"] == 0
    assert await _rows_under(session, tx["id"]) == []
    lines_resp = await client.get(f"/api/transactions/{tx['id']}/split", headers=auth_headers)
    assert lines_resp.json() == []
    assert await _balance(client, auth_headers, account["id"]) == pytest.approx(-500.0)

    again = await client.delete(f"/api/transactions/{tx['id']}/split", headers=auth_headers)
    assert again.status_code == 400
    assert "not split" in again.json()["detail"]


@pytest.mark.asyncio
async def test_split_rejects_lines_that_do_not_add_up(client, auth_headers):
    _, insurance, invested, tx, lines = await _vul(client, auth_headers)

    lines[1]["amount"] = "200.00"
    short = await _split(client, auth_headers, tx["id"], lines)
    assert short.status_code == 400
    assert "sum to the transaction amount" in short.json()["detail"]

    one = await _split(client, auth_headers, tx["id"], [{"category_id": insurance["id"], "amount": "500.00"}])
    assert one.status_code == 422

    zero = await _split(
        client, auth_headers, tx["id"],
        [{"category_id": insurance["id"], "amount": "500.00"}, {"category_id": invested["id"], "amount": "0"}],
    )
    assert zero.status_code == 422

    foreign = await _split(
        client, auth_headers, tx["id"],
        [{"category_id": str(uuid.uuid4()), "amount": "250.00"}, {"category_id": invested["id"], "amount": "250.00"}],
    )
    assert foreign.status_code == 400
    assert foreign.json()["detail"] == "Category not found"

    uncategorized_line = await _split(
        client, auth_headers, tx["id"],
        [{"category_id": None, "amount": "250.00"}, {"category_id": invested["id"], "amount": "250.00"}],
    )
    assert uncategorized_line.status_code == 200, uncategorized_line.text
    assert uncategorized_line.json()["children"][0]["category_id"] is None


@pytest.mark.asyncio
async def test_split_guards(client, auth_headers, session: AsyncSession, test_workspace, test_user):
    account, insurance, invested, tx, lines = await _vul(client, auth_headers)

    unknown = await _split(client, auth_headers, str(uuid.uuid4()), lines)
    assert unknown.status_code == 404

    pending = await _transaction(client, auth_headers, account["id"], status="pending")
    resp = await _split(client, auth_headers, pending["id"], lines)
    assert resp.status_code == 400 and "posted" in resp.json()["detail"]

    await _split(client, auth_headers, tx["id"], lines)
    child_id = (await client.get(f"/api/transactions/{tx['id']}/split", headers=auth_headers)).json()[0]["id"]
    half = [{"category_id": insurance["id"], "amount": "125.00"}, {"category_id": invested["id"], "amount": "125.00"}]
    resp = await _split(client, auth_headers, child_id, half)
    assert resp.status_code == 400 and "split again" in resp.json()["detail"]

    other = await _account(client, auth_headers, "Savings")
    debit = await _transaction(client, auth_headers, account["id"], amount="80.00")
    credit = await _transaction(client, auth_headers, other["id"], amount="80.00", type="credit")
    linked = await client.post(
        "/api/transactions/link-transfer",
        headers=auth_headers,
        json={"transaction_ids": [debit["id"], credit["id"]]},
    )
    assert linked.status_code == 200, linked.text
    resp = await _split(client, auth_headers, debit["id"], [
        {"category_id": insurance["id"], "amount": "40.00"}, {"category_id": invested["id"], "amount": "40.00"},
    ])
    assert resp.status_code == 400 and "Transfers" in resp.json()["detail"]

    installment = await _transaction(client, auth_headers, account["id"], amount="90.00")
    row = await session.get(Transaction, uuid.UUID(installment["id"]))
    row.installment_number, row.total_installments = 1, 3
    await session.commit()
    resp = await _split(client, auth_headers, installment["id"], [
        {"category_id": insurance["id"], "amount": "45.00"}, {"category_id": invested["id"], "amount": "45.00"},
    ])
    assert resp.status_code == 400 and "Installment" in resp.json()["detail"]

    opening = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        account_id=uuid.UUID(account["id"]), description="Opening balance",
        amount=Decimal("100.00"), currency="USD", date=date.today(), type="credit",
        source="opening_balance", status="posted",
    )
    session.add(opening)
    await session.commit()
    resp = await _split(client, auth_headers, str(opening.id), [
        {"category_id": insurance["id"], "amount": "50.00"}, {"category_id": invested["id"], "amount": "50.00"},
    ])
    assert resp.status_code == 400 and "Opening balance" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_split_refuses_a_transaction_with_group_splits(client, auth_headers):
    account, insurance, invested, _, _ = await _vul(client, auth_headers)
    group = (await client.post(
        "/api/groups", headers=auth_headers,
        json={"name": "Trip", "kind": "social", "default_currency": "USD"},
    )).json()
    members = []
    for name in ("Me", "A"):
        members.append((await client.post(
            f"/api/groups/{group['id']}/members", headers=auth_headers,
            json={"name": name, "is_self": name == "Me"},
        )).json())
    shared = await _transaction(
        client, auth_headers, account["id"], amount="60.00",
        splits={"share_type": "equal", "splits": [{"group_member_id": m["id"]} for m in members]},
    )

    resp = await _split(client, auth_headers, shared["id"], [
        {"category_id": insurance["id"], "amount": "30.00"}, {"category_id": invested["id"], "amount": "30.00"},
    ])

    assert resp.status_code == 400
    assert "group split" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_primary_amount_is_shared_out_exactly(
    client, auth_headers, session: AsyncSession, test_workspace, test_user
):
    account = await _account(client, auth_headers)
    cats = [await _category(client, auth_headers, f"C{i}") for i in range(3)]
    parent = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        account_id=uuid.UUID(account["id"]), description="EUR purchase",
        amount=Decimal("100.00"), currency="EUR", date=date.today(), type="debit",
        source="manual", status="posted",
        amount_primary=Decimal("33.33"), fx_rate_used=Decimal("0.3333"),
    )
    session.add(parent)
    await session.commit()

    resp = await _split(client, auth_headers, str(parent.id), [
        {"category_id": cats[0]["id"], "amount": "33.33"},
        {"category_id": cats[1]["id"], "amount": "33.33"},
        {"category_id": cats[2]["id"], "amount": "33.34"},
    ])

    assert resp.status_code == 200, resp.text
    primaries = [Decimal(str(c["amount_primary"])) for c in resp.json()["children"]]
    assert sum(primaries) == Decimal("33.33")
    assert all(c["fx_rate_used"] == pytest.approx(0.3333) for c in resp.json()["children"])
    assert all(c["currency"] == "EUR" for c in resp.json()["children"])


@pytest.mark.asyncio
async def test_deleting_the_parent_removes_its_lines(client, auth_headers, session: AsyncSession):
    account, _, _, tx, lines = await _vul(client, auth_headers)
    await _split(client, auth_headers, tx["id"], lines)

    resp = await client.delete(f"/api/transactions/{tx['id']}", headers=auth_headers)

    assert resp.status_code == 204, resp.text
    assert await _rows_under(session, tx["id"]) == []
    assert await session.get(Transaction, uuid.UUID(tx["id"])) is None
    assert await _balance(client, auth_headers, account["id"]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Totals and filters see the lines, never the parent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_counts_lines_not_parent(client, auth_headers):
    account, _, _, tx, lines = await _vul(client, auth_headers)
    await _split(client, auth_headers, tx["id"], lines)

    resp = await client.get(
        f"/api/transactions?account_id={account['id']}&include_summary=true",
        headers=auth_headers,
    )

    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary["income"] == pytest.approx(0)
    assert summary["expense"] == pytest.approx(250)
    # The invested line is transfer-like, so it is the only excluded money;
    # the hidden parent does not show up as another 500.
    assert summary["excluded"] == pytest.approx(250)
    assert summary["invested"] == pytest.approx(250)


@pytest.mark.asyncio
async def test_uncategorized_filter_skips_split_parent(client, auth_headers):
    account, insurance, _, tx, _ = await _vul(client, auth_headers)
    await _split(client, auth_headers, tx["id"], [
        {"category_id": insurance["id"], "amount": "250.00"},
        {"category_id": None, "amount": "250.00"},
    ])

    resp = await client.get(
        f"/api/transactions?account_id={account['id']}&uncategorized=true",
        headers=auth_headers,
    )

    items = resp.json()["items"]
    assert [item["parent_transaction_id"] for item in items] == [tx["id"]]
    assert items[0]["category_id"] is None


# ---------------------------------------------------------------------------
# Edit, ignore and delete rules
# ---------------------------------------------------------------------------


async def _children(client, auth_headers, parent_id):
    return (await client.get(f"/api/transactions/{parent_id}/split", headers=auth_headers)).json()


@pytest.mark.asyncio
async def test_line_edits_are_limited_to_bookkeeping_fields(client, auth_headers):
    _, insurance, _, tx, lines = await _vul(client, auth_headers)
    await _split(client, auth_headers, tx["id"], lines)
    child = (await _children(client, auth_headers, tx["id"]))[1]

    for locked in ({"amount": "100.00"}, {"date": "2026-01-01"}, {"status": "pending"},
                   {"is_ignored": True}, {"exclude_from_pnl": True}, {"type": "credit"}):
        resp = await client.patch(f"/api/transactions/{child['id']}", headers=auth_headers, json=locked)
        assert resp.status_code == 400, (locked, resp.text)
        assert "split line" in resp.json()["detail"]

    resp = await client.patch(
        f"/api/transactions/{child['id']}", headers=auth_headers,
        json={"category_id": insurance["id"], "notes": "reclassified"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["category_id"] == insurance["id"]
    assert resp.json()["notes"] == "reclassified"


@pytest.mark.asyncio
async def test_parent_edits_propagate_or_are_refused(client, auth_headers):
    _, _, _, tx, lines = await _vul(client, auth_headers)
    await _split(client, auth_headers, tx["id"], lines)

    for locked in ({"amount": "600.00"}, {"is_ignored": False}, {"type": "credit"}, {"status": "pending"}):
        resp = await client.patch(f"/api/transactions/{tx['id']}", headers=auth_headers, json=locked)
        assert resp.status_code == 400, (locked, resp.text)
        assert "Remove the split" in resp.json()["detail"]

    resp = await client.patch(
        f"/api/transactions/{tx['id']}", headers=auth_headers,
        json={"date": "2026-01-15", "description": "NWM VUL PREMIUM", "exclude_from_pnl": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_ignored"] is True
    for child in await _children(client, auth_headers, tx["id"]):
        assert child["date"] == "2026-01-15"
        assert child["description"] == "NWM VUL PREMIUM"
        assert child["exclude_from_pnl"] is True


@pytest.mark.asyncio
async def test_ignore_toggle_and_delete_guards(client, auth_headers, session: AsyncSession):
    _, _, _, tx, lines = await _vul(client, auth_headers)
    await _split(client, auth_headers, tx["id"], lines)
    child_ids = [c["id"] for c in await _children(client, auth_headers, tx["id"])]

    parent_ignore = await client.patch(f"/api/transactions/{tx['id']}/ignore", headers=auth_headers)
    assert parent_ignore.status_code == 400 and "remove the split" in parent_ignore.json()["detail"]
    child_ignore = await client.patch(f"/api/transactions/{child_ids[0]}/ignore", headers=auth_headers)
    assert child_ignore.status_code == 400 and "Split lines" in child_ignore.json()["detail"]

    child_delete = await client.delete(f"/api/transactions/{child_ids[0]}", headers=auth_headers)
    assert child_delete.status_code == 400 and "remove the split" in child_delete.json()["detail"]

    lone_line = await client.post(
        "/api/transactions/bulk-delete", headers=auth_headers, json={"transaction_ids": child_ids[:1]}
    )
    assert lone_line.status_code == 200 and lone_line.json()["deleted"] == 0
    assert len(await _rows_under(session, tx["id"])) == 2

    whole = await client.post(
        "/api/transactions/bulk-delete", headers=auth_headers, json={"transaction_ids": [tx["id"]]}
    )
    assert whole.status_code == 200 and whole.json()["deleted"] == 1
    assert await _rows_under(session, tx["id"]) == []
    assert await session.get(Transaction, uuid.UUID(tx["id"])) is None


@pytest.mark.asyncio
async def test_bulk_categorize_skips_the_parent(client, auth_headers):
    _, insurance, _, tx, lines = await _vul(client, auth_headers)
    await _split(client, auth_headers, tx["id"], lines)
    child = (await _children(client, auth_headers, tx["id"]))[1]

    resp = await client.patch(
        "/api/transactions/bulk-categorize", headers=auth_headers,
        json={"transaction_ids": [tx["id"], child["id"]], "category_id": insurance["id"]},
    )

    assert resp.status_code == 200, resp.text
    parent = (await client.get(f"/api/transactions/{tx['id']}", headers=auth_headers)).json()
    assert parent["category_id"] is None
    assert (await _children(client, auth_headers, tx["id"]))[1]["category_id"] == insurance["id"]


@pytest.mark.asyncio
async def test_parent_never_becomes_a_transfer(client, auth_headers):
    account, _, _, tx, lines = await _vul(client, auth_headers)
    await _split(client, auth_headers, tx["id"], lines)
    other = await _account(client, auth_headers, "Savings")
    credit = await _transaction(client, auth_headers, other["id"], amount="500.00", type="credit")

    link = await client.post(
        "/api/transactions/link-transfer", headers=auth_headers,
        json={"transaction_ids": [tx["id"], credit["id"]]},
    )
    assert link.status_code == 400 and "remove the split first" in link.json()["detail"]

    counterpart = await client.post(
        f"/api/transactions/{tx['id']}/create-counterpart", headers=auth_headers,
        json={"to_account_id": other["id"]},
    )
    assert counterpart.status_code == 400 and "remove the split first" in counterpart.json()["detail"]

    candidates = await client.get(
        f"/api/transactions/{credit['id']}/transfer-candidates", headers=auth_headers
    )
    assert candidates.status_code == 200
    assert tx["id"] not in {c["id"] for c in candidates.json()}

