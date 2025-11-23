from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from edgewizard_pipeline import run_edge_pipeline
from billing import router as billing_router
from credits_manager import consume_credit_or_fail, NoCreditsError, get_credit_status

import io
from PIL import Image, ImageOps
import base64
import time

from pillow_heif import register_heif_opener

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

# Include billing routes (/billing/...)
app.include_router(billing_router)


@app.post("/edge")
async def process_edge(
    image: UploadFile = File(...),
    client_id: str = Header(..., alias="X-Client-Id"),
):
    """
    Main edge-processing endpoint.
    Consumes one credit per image (paid first, then free).
    Returns a PNG outline as Base64 data URL.
    """
    try:
        # First: try to consume a credit for this client
        try:
            consume_credit_or_fail(client_id)
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
    client_id: str = Header(..., alias="X-Client-Id"),
):
    """
    Returns the current credit status for this client:
    paid_credits, free_credits, total_credits.
    """
    try:
        status = get_credit_status(client_id)
        return JSONResponse(status)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not get credit status: {e}")


# Local testing: uvicorn main:app --reload
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000)
