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

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
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


@app.post("/preview")
async def preview(file: UploadFile = File(...), body_index: int = Form(None)):
    """STEP -> a tessellated mesh (binary STL bytes) for the 3D viewer ONLY.

    This is visual only — a coarser mesh than /slice uses, purely so the front-end
    can show the real shape instead of a bounding-box placeholder. The authoritative
    volume/area/watertight numbers used for pricing still come from /analyze
    (OpenCascade's analytic measurement), never from this tessellation, since a
    mesh approximation is always slightly less accurate on curved surfaces.

    `body_index` (optional): preview just one solid from a multi-body file that the
    client has split into separate parts — see analyze_step()'s `bodies_detail`.

    STL isn't accepted here — the browser already has the real mesh for STL uploads."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large.")

    name = (file.filename or "").lower()
    if not (name.endswith(".step") or name.endswith(".stp")):
        raise HTTPException(status_code=400, detail="Preview is for STEP files only.")

    try:
        # Coarser deflection (0.3mm) than /slice's default (0.1mm) — this mesh is
        # only ever drawn on screen, so a smaller/faster payload matters more than
        # surface-fidelity here.
        stl = geometry.tessellate_step_to_stl(data, deflection=0.3, body_index=body_index)
    except Exception as e:
        raise HTTPException(status_code=422, detail="Could not build a preview mesh: %s" % e)

    return Response(content=stl, media_type="application/octet-stream")


@app.post("/thinwall")
async def thinwall(
    file: UploadFile = File(...),
    min_wall_mm: float = Form(...),
    scale_factor: float = Form(1.0),   # mm-per-file-unit × scale% — measure at final printed size
    body_index: int = Form(None),      # one solid from a split multi-body STEP file
):
    """Approximate wall-thickness check (STL or STEP; any process) via ray casting on the
    tessellated, final-scaled mesh. Inherently approximate — see PHASE2-SERVER.md §5.3.
    Always returns 200; check the `ok` flag and skip the check (never hard-reject) if false.
    `min_wall_mm` is the per-material threshold — the client already knows this from its
    pricing config, so the server stays a pure measurement service with one source of truth."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large.")

    name = (file.filename or "").lower()
    try:
        if name.endswith(".step") or name.endswith(".stp"):
            stl = geometry.tessellate_step_to_stl(data, deflection=0.2, body_index=body_index)
        else:
            stl = data
        if abs(scale_factor - 1.0) > 1e-9:
            stl = geometry.scale_stl(stl, scale_factor)
    except Exception as e:
        return {"ok": False, "message": "Could not prepare mesh for thickness analysis: %s" % e}

    return geometry.thin_wall_analysis(stl, float(min_wall_mm))


@app.post("/slice")
async def slice_part(
    file: UploadFile = File(...),
    process: str = Form("FDM"),
    material: str = Form("PLA"),
    infill: int = Form(20),
    scale_factor: float = Form(1.0),   # mm-per-file-unit × scale% — slice at final size
    body_index: int = Form(None),      # one solid from a split multi-body STEP file
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
            stl = geometry.tessellate_step_to_stl(data, body_index=body_index)
        else:
            stl = data
        if abs(scale_factor - 1.0) > 1e-9:
            stl = geometry.scale_stl(stl, scale_factor)
    except Exception as e:
        return {"sliced": False, "message": "Could not prepare mesh for slicing: %s" % e}

    return slicer.slice_fdm(stl, infill_pct=int(infill), material=material)
