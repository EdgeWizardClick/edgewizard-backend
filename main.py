from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from edgewizard_pipeline import run_edge_pipeline

import io
from PIL import Image, ImageOps
import base64
import time

from pillow_heif import register_heif_opener
register_heif_opener()

app = FastAPI()

# --------------------------------------------------------
# CORS (offen für MVP – später auf Domain einschränken)
# --------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],         # später: ["https://edgewizard.click"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_TYPES = [
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/heic",
    "image/heif",
    "image/heic-sequence",
]

# --------------------------------------------------------
# POST /edge – Haupt-API für EdgeWizard
# --------------------------------------------------------
@app.post("/edge")
async def edge(image: UploadFile = File(...)):
    # Dateityp prüfen
    if image.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {image.content_type}",
        )

    try:
        # Datei in den Speicher lesen
        file_bytes = await image.read()
        img = Image.open(io.BytesIO(file_bytes))

        # EXIF-Orientation berücksichtigen (dreht Bilder korrekt)
        img = ImageOps.exif_transpose(img)

        # Sicherstellen, dass wir ein RGB-Image an die Pipeline übergeben
        pil_input = img.convert("RGB")

        # EdgeWizard-Pipeline ausführen
        pil_output = run_edge_pipeline(pil_input)

        # Ergebnis als PNG in Bytes umwandeln
        out_bytes = io.BytesIO()
        pil_output.save(out_bytes, format="PNG")
        out_bytes.seek(0)

        # Base64 data URL erzeugen
        base64_png = base64.b64encode(out_bytes.read()).decode("utf-8")
        data_url = f"data:image/png;base64,{base64_png}"

        # Kleine künstliche Verzögerung für konsistentes UX
        time.sleep(0.1)

        return JSONResponse({"result_data_url": data_url})

    except HTTPException:
        # Explizite HTTP-Fehler unverändert weitergeben
        raise
    except Exception as e:
        # Generischer Fehler für das Frontend
        raise HTTPException(status_code=500, detail=f"Processing failed: {e}")


# Lokales Testing: uvicorn main:app --reload
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
