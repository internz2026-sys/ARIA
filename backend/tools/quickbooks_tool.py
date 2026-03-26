"""QuickBooks API wrapper — revenue, expenses, invoicing."""

import httpx


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/json"}


async def get_revenue_summary(realm_id: str, access_token: str, start_date: str, end_date: str) -> dict:
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
