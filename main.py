from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from edgewizard_pipeline import run_edge_pipeline

import io
from PIL import Image
import base64
import time

app = FastAPI()

# --------------------------------------------------------
# CORS (open for MVP - later restrict to your domain)
# --------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],         # later: ["https://edgewizard.click"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------
# POST /edge - main API for EdgeWizard
# --------------------------------------------------------
@app.post("/edge")
async def edge(image: UploadFile = File(...)):
    # basic file type check
    if image.content_type not in ["image/png", "image/jpeg", "image/jpg", "image/webp"]:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    try:
        # read file into memory
        file_bytes = await image.read()
        pil_input = Image.open(io.BytesIO(file_bytes)).convert("RGB")

        # run EdgeWizard pipeline
        pil_output = run_edge_pipeline(pil_input)

        # convert to PNG bytes
        out_bytes = io.BytesIO()
        pil_output.save(out_bytes, format="PNG")
        out_bytes.seek(0)

        # encode as base64 data URL
        base64_png = base64.b64encode(out_bytes.read()).decode("utf-8")
        data_url = f"data:image/png;base64,{base64_png}"

        # small artificial delay for consistent UX
        time.sleep(1)

        return JSONResponse({"result_data_url": data_url})

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing failed: {e}")


# local testing: uvicorn main:app --reload
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
