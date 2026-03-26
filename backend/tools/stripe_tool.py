"""Stripe API wrapper — billing, subscriptions, invoicing."""

import os
import stripe

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")


async def create_customer(email: str, name: str) -> dict:
    customer = stripe.Customer.create(email=email, name=name)
    return {"customer_id": customer.id, "email": email}


async def create_subscription(customer_id: str, price_id: str) -> dict:
    subscription = stripe.Subscription.create(customer=customer_id, items=[{"price": price_id}], trial_period_days=14)
    return {"subscription_id": subscription.id, "status": subscription.status}


async def create_invoice(customer_id: str, items: list[dict]) -> dict:
    invoice = stripe.Invoice.create(customer=customer_id, auto_advance=True)
    for item in items:
        stripe.InvoiceItem.create(customer=customer_id, invoice=invoice.id, amount=item["amount"], currency="usd", description=item.get("description", ""))
    invoice.finalize_invoice()
    return {"invoice_id": invoice.id, "status": invoice.status, "amount_due": invoice.amount_due}


async def get_payment_status(invoice_id: str) -> dict:
    invoice = stripe.Invoice.retrieve(invoice_id)
    return {"invoice_id": invoice.id, "status": invoice.status, "paid": invoice.paid, "amount_due": invoice.amount_due}
