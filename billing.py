import os
from typing import Any

import stripe
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse

from auth import get_current_user
from credits_manager import add_paid_credits

router = APIRouter(prefix="/billing", tags=["billing"])

# ---------------------------------------------------------------------------
# Stripe-Konfiguration
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# Erfolgs- / Abbruch-URLs (Frontend, app.emergent)
STRIPE_SUCCESS_URL = os.getenv(
    "STRIPE_SUCCESS_URL",
    "https://image-edge-detect.preview.emergentagent.com/success",
)
STRIPE_CANCEL_URL = os.getenv(
    "STRIPE_CANCEL_URL",
    "https://image-edge-detect.preview.emergentagent.com/cancel",
)

if not STRIPE_SECRET_KEY:
    raise RuntimeError("STRIPE_SECRET_KEY is not set")

if not STRIPE_WEBHOOK_SECRET:
    raise RuntimeError("STRIPE_WEBHOOK_SECRET is not set")

stripe.api_key = STRIPE_SECRET_KEY

# Price-IDs je Plan (lesen zuerst aus ENV, sonst Fallback zu deinen Test-IDs)
PLAN_CONFIG = {
    "bronze": {
        "price_id": os.getenv("BRONZE_PRICE_ID", "price_1SWclaRZIveFaT9XZ9FjfeOV"),
        "credits": 50,
    },
    "silver": {
        "price_id": os.getenv("SILVER_PRICE_ID", "price_1SWcmiRZIveFaT9Xaf4nBj9e"),
        "credits": 250,
    },
    "gold": {
        "price_id": os.getenv("GOLD_PRICE_ID", "price_1SWcphRZIveFaT9X2sAsj9k9"),
        "credits": 500,
    },
}


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

from pydantic import BaseModel


class CheckoutSessionRequest(BaseModel):
    plan_id: str  # "bronze" | "silver" | "gold"


class CheckoutSessionResponse(BaseModel):
    checkout_url: str


# ---------------------------------------------------------------------------
# Helper: user_id aus current_user extrahieren (egal ob dict oder Pydantic)
# ---------------------------------------------------------------------------


def _extract_user_id(current_user: Any) -> str:
    """
    current_user kann z.B. ein Pydantic-Model oder ein dict sein.
    Wir normalisieren das und lesen 'user_id' heraus.
    """
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if hasattr(current_user, "user_id"):
        return str(current_user.user_id)

    if hasattr(current_user, "dict"):
        data = current_user.dict()
        if "user_id" in data:
            return str(data["user_id"])

    if isinstance(current_user, dict) and "user_id" in current_user:
        return str(current_user["user_id"])

    raise HTTPException(status_code=500, detail="user_id missing in auth payload")


# ---------------------------------------------------------------------------
# Endpoint: Checkout-Session erstellen (nutzt eingeloggte:n User:in)
# ---------------------------------------------------------------------------


@router.post(
    "/create-checkout-session",
    response_model=CheckoutSessionResponse,
)
async def create_checkout_session(
    body: CheckoutSessionRequest,
    current_user: Any = Depends(get_current_user),
):
    """
    Erstellt eine Stripe Checkout Session für den angegebenen Plan.
    Verwendet die user_id aus dem Auth-Token und schreibt sie in die
    Stripe-Metadaten. Damit werden Credits eindeutig dem Account zugeordnet.
    """
    plan_id = body.plan_id
    config = PLAN_CONFIG.get(plan_id)

    if not config:
        raise HTTPException(status_code=400, detail="Unknown plan_id")

    price_id = config["price_id"]

    user_id = _extract_user_id(current_user)

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price": price_id,
                    "quantity": 1,
                }
            ],
            success_url=f"{STRIPE_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=STRIPE_CANCEL_URL,
            metadata={
                # WICHTIG: Credits laufen jetzt über user_id, nicht mehr über Device-ID
                "user_id": user_id,
                "plan_id": plan_id,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {e}")

    return CheckoutSessionResponse(checkout_url=session.url)


# ---------------------------------------------------------------------------
# Endpoint: Stripe Webhook
# ---------------------------------------------------------------------------


@router.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    """
    Stripe Webhook für checkout.session.completed.

    Erwartet Metadaten:
      - user_id: UUID aus unserem Auth-System
      - plan_id: 'bronze' | 'silver' | 'gold'

    Fallback für alte Sessions:
      - client_id (Device ID), wird nur noch geloggt.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if sig_header is None:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature header")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")

    event_type = event.get("type")
    obj = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        session_id = obj.get("id")
        metadata = obj.get("metadata") or {}

        user_id = metadata.get("user_id")
        plan_id = metadata.get("plan_id")

        # Legacy-Fallback: alte Webhooks mit client_id (Device)
        legacy_client_id = metadata.get("client_id")

        print("Stripe webhook received: checkout.session.completed")
        print(f"Checkout completed - Session: {session_id}")
        print(f"Metadata user_id: {user_id} plan_id: {plan_id}")

        if legacy_client_id and not user_id:
            print(
                f"Legacy checkout (client_id={legacy_client_id}) ohne user_id – "
                f"Credits werden NICHT auf Device gebucht."
            )

        config = PLAN_CONFIG.get(plan_id or "")
        if not config:
            print(f"Unknown plan_id in webhook: {plan_id}")
            return JSONResponse({"status": "ignored"}, status_code=200)

        credits_to_add = config["credits"]

        if user_id:
            # Credits im neuen System auf user_id buchen
            add_paid_credits(user_id, credits_to_add)
            print(f"Added {credits_to_add} credits to user {user_id}")
        else:
            print("No user_id in metadata – skipping credit assignment.")

    else:
        # Andere Webhooks aktuell ignorieren
        print(f"Unhandled Stripe event type: {event_type}")

    return JSONResponse({"status": "ok"}, status_code=200)
