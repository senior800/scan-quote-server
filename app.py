"""
S-CAN quoting geometry service — FastAPI app (Phase 2, milestone 2a).

Endpoints
---------
GET  /health           liveness probe
POST /analyze          multipart upload (field: file) -> geometry JSON

The Phase-1 front-end calls /analyze in place of its in-browser parser, then
computes the price client-side from pricing-config.json (unchanged). Pricing,
thin-wall, the slicer, the job queue and the WooCommerce connector are later
milestones — see ../PHASE2-SERVER.md.
"""

import os

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import geometry
import slicer

MAX_BYTES = 120 * 1024 * 1024  # 120 MB upload cap (matches the front-end limit)

app = FastAPI(title="S-CAN quoting — geometry service", version="0.1.0")

# Set ALLOWED_ORIGIN to your site in production, e.g. "https://s-can.co.uk"
# (comma-separated for several). Defaults to "*" for local dev.
_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGIN", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True, "service": "scan-geometry", "version": app.version}


@app.post("/analyze")
async def analyze(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large.")

    result = geometry.analyze(file.filename or "", data)
    result["filename"] = file.filename
    result["bytes"] = len(data)
    return result


@app.post("/slice")
async def slice_part(
    file: UploadFile = File(...),
    process: str = Form("FDM"),
    material: str = Form("PLA"),
    infill: int = Form(20),
    scale_factor: float = Form(1.0),   # mm-per-file-unit × scale% — slice at final size
):
    """FDM only — returns slicer-measured time + filament mass (milestone 2b).
    SLA/SLS are priced on volume + bounding box, so they short-circuit here.
    Always returns 200; check the `sliced` flag and fall back to the estimate if false."""
    if (process or "FDM").upper() != "FDM":
        return {"sliced": False, "message": "SLA/SLS are priced on volume + bounding box (no slicing)."}

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large.")

    name = (file.filename or "").lower()
    try:
        if name.endswith(".step") or name.endswith(".stp"):
            stl = geometry.tessellate_step_to_stl(data)
        else:
            stl = data
        if abs(scale_factor - 1.0) > 1e-9:
            stl = geometry.scale_stl(stl, scale_factor)
    except Exception as e:
        return {"sliced": False, "message": "Could not prepare mesh for slicing: %s" % e}

    return slicer.slice_fdm(stl, infill_pct=int(infill), material=material)
