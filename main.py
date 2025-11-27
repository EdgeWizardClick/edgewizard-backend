from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from edgewizard_pipeline import run_edge_pipeline
from billing import router as billing_router
from credits_manager import (consume_credit_or_fail, NoCreditsError, get_credit_status, get_credit_status_with_reset_info)

import io
from PIL import Image, ImageOps
import base64
import time

from pillow_heif import register_heif_opener

from auth import router as auth_router, get_current_user

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
        result_image = run_edge_pipeline(pil_image)

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
    current_user = Depends(get_current_user),
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
    current_user = Depends(get_current_user),
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

@app.get("/me/credits/status")
async def me_credits_status(
    current_user = Depends(get_current_user),
):
    """
    Returns detailed credit status including timing information for the
    next possible free-credit refill.

    This endpoint does NOT change the underlying credit logic:
      - free credits do not stack
      - free credits are only refilled once per day
      - if paid_credits > 0, no free credits are given
    """
    try:
        # Bestehenden Status aus der existierenden Logik lesen
        status = get_credit_status(current_user.user_id)

        # Serverzeit bestimmen, bevorzugt Europe/Zurich
        now = datetime.utcnow()
        if ZoneInfo is not None:
            try:
                tz = ZoneInfo("Europe/Zurich")
                now = datetime.now(tz)
            except Exception:
                # Fallback: UTC
                pass

        paid = int(status.get("paid_credits", 0))

        if paid > 0:
            # Wenn bezahlte Credits vorhanden sind, kein Free-Credit-Timer
            next_free_refill_at = None
        else:
            # Nächstes Mitternacht in der Zielzeitzone (potenzieller Free-Drop)
            tomorrow = now.date() + timedelta(days=1)
            next_midnight = datetime.combine(
                tomorrow,
                dt_time(0, 0, 0),
                tzinfo=now.tzinfo,
            )
            next_free_refill_at = next_midnight.isoformat()

        return JSONResponse(
            {
                **status,
                "next_free_refill_at": next_free_refill_at,
                "server_now": now.isoformat(),
            }
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not get detailed credit status: {e}",
        )


@app.get("/me/credits/status")
async def me_credits_status(
    current_user = Depends(get_current_user),
):
    """
    Returns detailed credit status including timing information for the
    next possible free-credit refill.

    This endpoint does NOT change the underlying credit logic:
      - free credits do not stack
      - free credits are only refilled once per day
      - if paid_credits > 0, no free credits are given
    """
    try:
        # Use the existing credit logic
        status = get_credit_status(current_user.user_id)

        # Determine server time, preferring Europe/Zurich
        now = datetime.utcnow()
        if ZoneInfo is not None:
            try:
                tz = ZoneInfo("Europe/Zurich")
                now = datetime.now(tz)
            except Exception:
                # Fallback: UTC
                pass

        paid = int(status.get("paid_credits", 0))

        if paid > 0:
            # When there are paid credits, do not expose a free-credit timer
            next_free_refill_at = None
        else:
            # Next midnight in the target timezone (potential free-drop)
            tomorrow = now.date() + timedelta(days=1)
            next_midnight = datetime.combine(
                tomorrow,
                dt_time(0, 0, 0),
                tzinfo=now.tzinfo,
            )
            next_free_refill_at = next_midnight.isoformat()

        return JSONResponse(
            {
                **status,
                "next_free_refill_at": next_free_refill_at,
                "server_now": now.isoformat(),
            }
        )
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

# Local testing: uvicorn main:app --reload
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000)








