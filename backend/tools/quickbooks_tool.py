"""QuickBooks API wrapper — revenue, expenses, invoicing."""

import re

import httpx

# QuickBooks accepts dates in YYYY-MM-DD format. Anything else is rejected
# upstream anyway, but we validate locally so a hostile/typo'd input
# can't break out of the WHERE clause via single-quote injection
# (e.g. `2024-01-01' OR '1'='1` → arbitrary query). Audit item #14.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date(value: str, field: str) -> str:
    """Allow ISO-8601 dates only. Raises ValueError on anything else.

    QuickBooks' query syntax uses single-quoted string literals, and the
    callers of this module pass `start_date` / `end_date` straight into
    f-strings. Without validation, an attacker controlling the date
    inputs could close the quote, append `OR 1=1`, and read every
    Invoice/Purchase the OAuth token can see (not ARIA's DB, but still
    cross-row data leakage at the QB layer).
    """
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f"{field} must be ISO date YYYY-MM-DD; got {value!r}")
    return value


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/json"}


async def get_revenue_summary(realm_id: str, access_token: str, start_date: str, end_date: str) -> dict:
    start_date = _validate_date(start_date, "start_date")
    end_date = _validate_date(end_date, "end_date")
    async with httpx.AsyncClient() as client:
        query = f"SELECT * FROM Invoice WHERE TxnDate >= '{start_date}' AND TxnDate <= '{end_date}'"
        resp = await client.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
            params={"query": query},
            headers=_headers(access_token),
        )
        resp.raise_for_status()
        return resp.json()


async def get_expenses(realm_id: str, access_token: str, start_date: str, end_date: str) -> dict:
    start_date = _validate_date(start_date, "start_date")
    end_date = _validate_date(end_date, "end_date")
    async with httpx.AsyncClient() as client:
        query = f"SELECT * FROM Purchase WHERE TxnDate >= '{start_date}' AND TxnDate <= '{end_date}'"
        resp = await client.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
            params={"query": query},
            headers=_headers(access_token),
        )
        resp.raise_for_status()
        return resp.json()


async def create_invoice(realm_id: str, access_token: str, customer_ref: str, items: list[dict]) -> dict:
    line_items = [
        {"Amount": item["amount"], "DetailType": "SalesItemLineDetail",
         "SalesItemLineDetail": {"ItemRef": {"value": item.get("item_id", "1")}}}
        for item in items
    ]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice",
            json={"CustomerRef": {"value": customer_ref}, "Line": line_items},
            headers=_headers(access_token),
        )
        resp.raise_for_status()
        return resp.json()
