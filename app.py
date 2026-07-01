"""
S-CAN quoting geometry service — FastAPI app (Phase 2).

Endpoints
---------
GET  /health           liveness probe (never gated/rate-limited)
POST /analyze          multipart upload (field: file) -> geometry JSON
POST /preview          STEP -> tessellated STL bytes for the 3D viewer
POST /thinwall         STL/STEP -> approximate wall-thickness check
POST /slice            FDM only -> OrcaSlicer-measured print time + filament mass

Ops hardening (this file)
-------------------------
The heavy geometry/slicer work is SYNCHRONOUS and CPU/RAM-heavy, and this runs on
a small (1-CPU / 2 GB) droplet exposed to public uploads. Three protections:

  1. Off-load blocking work to a threadpool (run_in_threadpool) so the event loop
     stays responsive — critically, /health keeps answering during a long slice, so
     Docker's healthcheck doesn't kill the container mid-slice.
  2. Concurrency caps (asyncio semaphores): at most HEAVY_MAX heavy requests at once,
     and slicing serialised to ONE at a time (it's the RAM hog — two concurrent slices
     would blow the 2 GB box even with swap). Over the cap -> 503 (shed load), so a
     burst/abuse can't pile up 40 concurrent slices and OOM the machine.
  3. Simple in-memory per-IP rate limiting -> 429.

All tunables are env-overridable. Pricing stays client-side; the WooCommerce
connector and automatic mesh repair are later milestones — see ../PHASE2-SERVER.md.
"""

import os
import time
import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool

import geometry
import slicer

MAX_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(120 * 1024 * 1024)))  # 120 MB (matches front-end + Caddy)

# --- concurrency caps -------------------------------------------------------
HEAVY_MAX = int(os.getenv("HEAVY_MAX", "2"))              # max concurrent heavy requests overall
ACQUIRE_WAIT_S = int(os.getenv("ACQUIRE_WAIT_S", "25"))   # wait this long for a slot, else 503
_HEAVY = asyncio.Semaphore(HEAVY_MAX)
_SLICE = asyncio.Semaphore(1)                             # slicing is RAM-heavy — never two at once

# --- rate limiting ----------------------------------------------------------
RL_WINDOW_S = int(os.getenv("RL_WINDOW_S", "60"))
RL_MAX = int(os.getenv("RL_MAX", "60"))                   # requests per window per IP across heavy endpoints
_RL_PATHS = ("/analyze", "/preview", "/thinwall", "/slice")
_rl = defaultdict(deque)


def _log(msg):
    print("[app] " + msg, flush=True)


@asynccontextmanager
async def _heavy_slot(slice_job=False):
    """Admit a heavy request only if a slot is free within ACQUIRE_WAIT_S, else 503.
    Slicing additionally takes the single SLICE slot so two slices never overlap.
    Slots are released only when the work actually finishes (we never abandon a
    running threadpool job), so the semaphores reflect real in-flight CPU/RAM use."""
    try:
        await asyncio.wait_for(_HEAVY.acquire(), timeout=ACQUIRE_WAIT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="Server busy — please retry in a moment.")
    got_slice = False
    try:
        if slice_job:
            try:
                await asyncio.wait_for(_SLICE.acquire(), timeout=ACQUIRE_WAIT_S)
                got_slice = True
            except asyncio.TimeoutError:
                raise HTTPException(status_code=503, detail="Server busy slicing — please retry in a moment.")
        yield
    finally:
        if got_slice:
            _SLICE.release()
        _HEAVY.release()


async def _rate_limit(request: Request, call_next):
    """Fixed-window per-IP limiter on the heavy POST endpoints. In-memory (single
    uvicorn worker); fine for this low-traffic service. Client IP comes from
    X-Forwarded-For (Caddy sets it) since request.client is the proxy."""
    if request.method == "POST" and request.url.path in _RL_PATHS:
        xff = request.headers.get("x-forwarded-for", "")
        ip = (xff.split(",")[0].strip() if xff else "") or (request.client.host if request.client else "?")
        now = time.monotonic()
        dq = _rl[ip]
        while dq and now - dq[0] > RL_WINDOW_S:
            dq.popleft()
        if len(dq) >= RL_MAX:
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded — please slow down."})
        dq.append(now)
        if len(_rl) > 10000:            # crude unbounded-growth guard against many-IP abuse
            for k in [k for k, v in list(_rl.items()) if not v]:
                _rl.pop(k, None)
    return await call_next(request)


# --- blocking work, kept OUT of the async handlers so it runs in the threadpool ---
def _prep_mesh(name, data, deflection, scale_factor, body_index):
    """STEP -> tessellated STL (at `deflection`); STL passes through. Then scale to
    the final printed size. Returns raw STL bytes. Raises on failure (caller maps it)."""
    if name.endswith(".step") or name.endswith(".stp"):
        stl = geometry.tessellate_step_to_stl(data, deflection=deflection, body_index=body_index)
    else:
        stl = data
    if abs(scale_factor - 1.0) > 1e-9:
        stl = geometry.scale_stl(stl, scale_factor)
    return stl


def _thinwall_sync(name, data, min_wall_mm, scale_factor, body_index):
    try:
        stl = _prep_mesh(name, data, 0.2, scale_factor, body_index)
    except Exception as e:
        return {"ok": False, "message": "Could not prepare mesh for thickness analysis: %s" % e}
    return geometry.thin_wall_analysis(stl, float(min_wall_mm))


def _slice_sync(name, data, infill, material, scale_factor, body_index):
    try:
        stl = _prep_mesh(name, data, 0.1, scale_factor, body_index)   # slicer default deflection
    except Exception as e:
        return {"sliced": False, "message": "Could not prepare mesh for slicing: %s" % e}
    return slicer.slice_fdm(stl, infill_pct=int(infill), material=material)


app = FastAPI(title="S-CAN quoting — geometry service", version="0.2.0")

# Middleware order matters: the LAST one added is the OUTERMOST layer. Add the rate
# limiter FIRST and CORS LAST so CORS wraps everything — otherwise a 429/503 from the
# limiter would reach the browser WITHOUT CORS headers and surface as an opaque CORS
# error instead of a readable status the client can fall back from.
app.add_middleware(BaseHTTPMiddleware, dispatch=_rate_limit)

# Set ALLOWED_ORIGIN to your site in production, e.g. "https://s-can.co.uk"
# (comma-separated for several). Defaults to "*" for local dev.
_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGIN", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


async def _read_capped(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large.")
    return data


@app.get("/health")
def health():
    return {"ok": True, "service": "scan-geometry", "version": app.version}


@app.post("/analyze")
async def analyze(file: UploadFile = File(...)):
    data = await _read_capped(file)
    async with _heavy_slot():
        result = await run_in_threadpool(geometry.analyze, file.filename or "", data)
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
    data = await _read_capped(file)

    name = (file.filename or "").lower()
    if not (name.endswith(".step") or name.endswith(".stp")):
        raise HTTPException(status_code=400, detail="Preview is for STEP files only.")

    try:
        # Coarser deflection (0.3mm) than /slice's default (0.1mm) — this mesh is
        # only ever drawn on screen, so a smaller/faster payload matters more than
        # surface-fidelity here.
        async with _heavy_slot():
            stl = await run_in_threadpool(geometry.tessellate_step_to_stl, data, 0.3, body_index)
    except HTTPException:
        raise
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
    data = await _read_capped(file)
    name = (file.filename or "").lower()
    async with _heavy_slot():
        return await run_in_threadpool(_thinwall_sync, name, data, min_wall_mm, scale_factor, body_index)


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

    data = await _read_capped(file)
    name = (file.filename or "").lower()
    async with _heavy_slot(slice_job=True):
        return await run_in_threadpool(_slice_sync, name, data, infill, material, scale_factor, body_index)
