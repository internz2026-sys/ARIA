"""Shopify API wrapper — orders, abandoned carts, inventory."""

import httpx


def _headers(access_token: str) -> dict:
    return {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}


async def get_orders(store_url: str, access_token: str, since_date: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://{store_url}/admin/api/2024-01/orders.json",
            params={"created_at_min": since_date, "status": "any"},
            headers=_headers(access_token),
        )
        resp.raise_for_status()
        return resp.json()


async def get_abandoned_carts(store_url: str, access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://{store_url}/admin/api/2024-01/checkouts.json",
            headers=_headers(access_token),
        )
        resp.raise_for_status()
        return resp.json()


async def get_inventory(store_url: str, access_token: str, sku: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://{store_url}/admin/api/2024-01/products.json",
            params={"fields": "id,title,variants"},
            headers=_headers(access_token),
        )
        resp.raise_for_status()
        products = resp.json().get("products", [])
        for p in products:
            for v in p.get("variants", []):
                if v.get("sku") == sku:
                    return {"sku": sku, "inventory_quantity": v.get("inventory_quantity", 0), "product": p["title"]}
        return {"sku": sku, "inventory_quantity": 0, "product": "Not found"}
