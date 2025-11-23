# billing.py

import os
import stripe
from fastapi import APIRouter, HTTPException, Request, Header
from pydantic import BaseModel
from typing import Literal, Optional
from dotenv import load_dotenv

# .env laden
load_dotenv()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL")
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL")

if not STRIPE_SECRET_KEY:
    raise RuntimeError("STRIPE_SECRET_KEY is missing - check your .env file.")

stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter(
    prefix="/billing",
    tags=["billing"],
)

# Mapping unserer Pläne ? Price-IDs
PLAN_TO_PRICE_ID = {
    "bronze": "price_1SWclaRZIveFaT9XZ9FjfeOV",
    "silver": "price_1SWcmiRZIveFaT9Xaf4nBj9e",
    "gold":  "price_1SWcphRZIveFaT9X2sAsj9k9",
}

class CheckoutSessionRequest(BaseModel):
    plan_id: Literal["bronze", "silver", "gold"]

class CheckoutSessionResponse(BaseModel):
    checkout_url: str

@router.post("/create-checkout-session", response_model=CheckoutSessionResponse)
async def create_checkout_session(payload: CheckoutSessionRequest):
    price_id = PLAN_TO_PRICE_ID.get(payload.plan_id)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid plan_id")

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
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

    return CheckoutSessionResponse(checkout_url=session.url)

@router.post("/stripe-webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(None, alias="Stripe-Signature")
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

    event_type = event["type"]
    print("Stripe webhook received:", event_type)

    if event_type == "checkout.session.completed":
        data = event["data"]["object"]
        print("Checkout completed - Session:", data.get("id"))

    return {"status": "ok"}
