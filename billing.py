# billing.py

import os
from typing import Literal, Optional

import stripe
from fastapi import APIRouter, HTTPException, Request, Header
from pydantic import BaseModel
from dotenv import load_dotenv

from credits_manager import add_paid_credits

load_dotenv()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL")
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL")

if not STRIPE_SECRET_KEY:
    raise RuntimeError("STRIPE_SECRET_KEY is missing - check your environment.")

stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter(
    prefix="/billing",
    tags=["billing"],
)

# Mapping plans to Stripe price IDs
PLAN_TO_PRICE_ID = {
    "bronze": "price_1SWclaRZIveFaT9XZ9FjfeOV",
    "silver": "price_1SWcmiRZIveFaT9Xaf4nBj9e",
    "gold": "price_1SWcphRZIveFaT9X2sAsj9k9",
}

# Mapping plans to credits
PLAN_TO_CREDITS = {
    "bronze": 50,
    "silver": 250,
    "gold": 500,
}


class CheckoutSessionRequest(BaseModel):
    plan_id: Literal["bronze", "silver", "gold"]


class CheckoutSessionResponse(BaseModel):
    checkout_url: str


@router.post("/create-checkout-session", response_model=CheckoutSessionResponse)
async def create_checkout_session(
    payload: CheckoutSessionRequest,
    client_id: str = Header(..., alias="X-Client-Id"),
):
    price_id = PLAN_TO_PRICE_ID.get(payload.plan_id)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid plan_id")

    if not STRIPE_SUCCESS_URL or not STRIPE_CANCEL_URL:
        raise HTTPException(status_code=500, detail="Stripe redirect URLs not configured")

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price": price_id,
                    "quantity": 1,
                }
            ],
            success_url=f"{STRIPE_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=STRIPE_CANCEL_URL,
            metadata={
                "client_id": client_id,
                "plan_id": payload.plan_id,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

    return CheckoutSessionResponse(checkout_url=session.url)


@router.post("/stripe-webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(None, alias="Stripe-Signature"),
):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=stripe_signature,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {str(e)}")

    event_type = event.get("type")
    print("Stripe webhook received:", event_type)

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]

        metadata = session.get("metadata") or {}
        client_id = metadata.get("client_id")
        plan_id = metadata.get("plan_id")

        print("Checkout completed - Session:", session.get("id"))
        print("Metadata client_id:", client_id, "plan_id:", plan_id)

        if client_id and plan_id:
            credits = PLAN_TO_CREDITS.get(plan_id)
            if credits:
                add_paid_credits(client_id, credits)
                print(f"Added {credits} credits to client {client_id}")

    return {"status": "ok"}
