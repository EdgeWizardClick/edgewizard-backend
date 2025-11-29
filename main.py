from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from edgewizard_pipeline import run_edge_pipeline
from billing import router as billing_router
from credits_manager import (
    consume_credit_or_fail,
    NoCreditsError,
    get_credit_status,
    get_credit_status_with_reset_info,
    add_paid_credits,
)

import io
import os
import base64
import time

from pydantic import BaseModel
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from auth import router as auth_router, get_current_user, get_user_by_email

# Load optional timezone info only if needed in future
try:
    from zoneinfo import ZoneInfo  # noqa: F401
except ImportError:
    ZoneInfo = None  # type: ignore

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")

register_heif_opener()

app = FastAPI()

# CORS configuration (allow app.emergent preview and general web usage)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # can be restricted later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include auth routes (/auth/...)
app.include_router(auth_router, prefix="/auth", tags=["auth"])

# Include billing routes (/billing/...)
app.include_router(billing_router)


@app.post("/edge")
async def process_edge(
    image: UploadFile = File(...),
    outline: bool = Form(False),
    current_user = Depends(get_current_user),
):
    """
    Main edge-processing endpoint.
    Consumes one credit per image (paid first, then free).
    Returns a PNG outline as Base64 data URL.
    Credits sind jetzt an user_id (Account) gebunden.
    """
    user_id = current_user.user_id

    try:
        # Credit consumption for this account
        try:
            consume_credit_or_fail(user_id)
        except NoCreditsError:
            # No credits available
            raise HTTPException(
                status_code=402,
                detail={
                    "detail": "NO_CREDITS",
                    "message": "No credits left. Visit the Shop to continue processing images.",
                },
            )

        # Read file content
        file_bytes = await image.read()

        # Open as PIL image and fix EXIF orientation
        pil_image = Image.open(io.BytesIO(file_bytes))
        pil_image = ImageOps.exif_transpose(pil_image)

        # Run the high-quality edge pipeline
        result_image = run_edge_pipeline(pil_image, enable_border=outline)


        # Encode result as PNG Base64 data URL
        buffer = io.BytesIO()
        result_image.save(buffer, format="PNG")
        png_bytes = buffer.getvalue()
        base64_data = base64.b64encode(png_bytes).decode("ascii")
        data_url = f"data:image/png;base64,{base64_data}"

        # Small artificial delay (as before)
        time.sleep(0.1)

        return JSONResponse({"result_data_url": data_url})

    except HTTPException:
        # Re-raise explicit HTTP errors
        raise
    except Exception as e:
        # Generic error for frontend
        raise HTTPException(status_code=500, detail=f"Processing failed: {e}")


@app.get("/me/credits")
async def me_credits(
    current_user=Depends(get_current_user),
):
    """
    Returns the current credit status for the logged-in user:
    paid_credits, free_credits, total_credits.
    Credits sind jetzt user_id-basiert.
    """
    try:
        status = get_credit_status(current_user.user_id)
        return JSONResponse(status)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not get credit status: {e}")


@app.get("/me/credits/status")
async def me_credits_status(
    current_user=Depends(get_current_user),
):
    """
    Returns detailed credit status for the logged-in user, including timing
    information for the next possible free-credit refill.

    This endpoint does NOT change the underlying credit logic. It only reads:
      - paid_credits
      - free_credits
      - total_credits
      - next_free_refill_at (ISO) or None if paid credits exist
      - server_now (ISO, Europe/Zurich if available)
    """
    try:
        status = get_credit_status_with_reset_info(current_user.user_id)
        return JSONResponse(status)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not get detailed credit status: {e}",
        )


class GrantCreditsRequest(BaseModel):
    user_id: str
    credits: int


@app.post("/admin/grant-credits")
async def admin_grant_credits(
    payload: GrantCreditsRequest,
    request: Request,
):
    """
    Admin-only endpoint to grant paid credits to a user.

    Security:
      - Requires the HTTP header "x-admin-key" to match ADMIN_API_KEY from the environment.
      - Should only be used by the owner of EdgeWizard for support / manual corrections.
    """
    if not ADMIN_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_API_KEY is not configured on the server.",
        )

    api_key = request.headers.get("x-admin-key")
    if api_key != ADMIN_API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Forbidden: invalid admin key.",
        )

    if payload.credits <= 0:
        raise HTTPException(
            status_code=400,
            detail="credits must be a positive integer.",
        )

    try:
        add_paid_credits(payload.user_id, payload.credits)
        # Return the updated status for convenience
        status = get_credit_status(payload.user_id)
        return JSONResponse(
            {
                "message": "Credits granted successfully.",
                "user_id": payload.user_id,
                "granted_credits": payload.credits,
                "updated_status": status,
            }
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not grant credits: {e}",
        )


class GrantCreditsByEmailRequest(BaseModel):
    email: str
    credits: int


@app.post("/admin/grant-credits-by-email")
async def admin_grant_credits_by_email(
    payload: GrantCreditsByEmailRequest,
    request: Request,
):
    """
    Admin-only endpoint to grant paid credits to a user, identified by email.

    Security:
      - Requires the HTTP header "x-admin-key" to match ADMIN_API_KEY from the environment.
      - Intended for support use (manual corrections, promo credits, etc.).
    """
    if not ADMIN_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_API_KEY is not configured on the server.",
        )

    api_key = request.headers.get("x-admin-key")
    if api_key != ADMIN_API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Forbidden: invalid admin key.",
        )

    if payload.credits <= 0:
        raise HTTPException(
            status_code=400,
            detail="credits must be a positive integer.",
        )

    # Look up user by email
    user = get_user_by_email(payload.email)
    if user is None:
        raise HTTPException(
            status_code=404,
            detail=f"User with email {payload.email} not found.",
        )

    try:
        status = add_paid_credits(user.user_id, payload.credits)
        return JSONResponse(
            {
                "message": "Credits granted successfully.",
                "user_id": user.user_id,
            "email": str(user.email),
                "granted_credits": payload.credits,
                "updated_status": status,
            }
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not grant credits by email: {e}",
        )


class ResetPasswordRequest(BaseModel):
    email: str
    new_password: str


@app.post("/admin/reset-password")
async def admin_reset_password_route(
    payload: ResetPasswordRequest,
    request: Request,
):
    """
    Admin-only endpoint to reset a user's password by email.
    Uses the same ADMIN_API_KEY protection as /admin/grant-credits.
    """
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured")

    api_key = request.headers.get("x-admin-key")
    if api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid admin key")

    try:
        from auth import admin_reset_password
        uid = admin_reset_password(payload.email, payload.new_password)
        return {"message": "Password reset successful", "user_id": uid}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# Local testing: uvicorn main:app --reload
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000)





