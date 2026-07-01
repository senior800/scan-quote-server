# S-CAN quoting — geometry service (Phase 2, milestone 2a)

A small self-hosted service that ingests **STL and STEP** and returns the geometry
the quoting front-end needs. This is milestone **2a** of [../PHASE2-SERVER.md](../PHASE2-SERVER.md):
the headline new capability is **STEP support** (via OpenCascade), which a browser can't do.

## What it does (2a)

`POST /analyze` (multipart, field `file`) → JSON:

```json
{
  "valid": true, "format": "STEP", "units_known": true,
  "volume_cm3": 12.4, "area_cm2": 38.6,
  "bbox_mm": {"x": 40.0, "y": 40.0, "z": 10.0},
  "bodies": 1, "watertight": true, "triangles": null,
  "filename": "bracket.step", "bytes": 10456, "message": ""
}
```

- **STL** (trimesh): volume, surface area, bounding box, watertight, body count. Unitless → `units_known: false`, the front-end keeps its mm/cm/inch + scale control.
- **STEP** (OpenCascade): same, read as a real B-rep solid, **forced to millimetres** → `units_known: true` (front-end can lock units to mm). Surface-only / no-solid STEP is rejected to the "submit for review" path.
- Invalid/unreadable files return `{"valid": false, "message": ...}` — never a stack trace.

Pricing stays in the front-end (config-driven) for now; it moves server-side in **2b** when OrcaSlicer gives authoritative FDM time/mass.

## Real shape preview (`POST /preview`)

`/analyze` deliberately returns only summary numbers, so the front-end initially shows
a plain box sized to the bounding box. `POST /preview` (multipart, field `file`,
**STEP only**) returns a **tessellated mesh** (binary STL bytes) purely so the viewer
can draw the real shape instead:

```bash
curl -F "file=@part.step" https://quote-api.s-can.co.uk/preview -o preview.stl
```

- **Visual only** — uses a coarser tessellation (0.3mm) than `/slice` (0.1mm), since it's
  only ever drawn on screen. The **pricing-relevant numbers stay from `/analyze`**
  (OpenCascade's analytic measurement) — this mesh never overwrites them, because a
  tessellation is always a slightly-approximate stand-in for the true curved surface.
- The front-end calls this automatically, silently, right after `/analyze` — if it's
  slow or fails, the part just keeps showing the bounding-box placeholder. Nothing
  else depends on it working.

## Run it (Docker — the only supported way; OpenCascade needs conda)

```bash
cd server
docker build -t scan-geometry .
docker run --rm -p 8000:8000 scan-geometry
```

Then:

```bash
curl http://localhost:8000/health
curl -F "file=@/path/to/part.step" http://localhost:8000/analyze
curl -F "file=@/path/to/part.stl"  http://localhost:8000/analyze
```

> **Note:** this was authored in a Windows dev environment with no Python/conda, so it has **not been executed here** — first run is on a Docker host (your machine or the droplet). Most likely first-run snag is a pythonocc API name across versions; `environment.yml` pins `pythonocc-core=7.8.1.1` to match the API used in `geometry.py` (`brepgprop.VolumeProperties`, `brepbndlib.AddOptimal`). If you bump the version and imports break, that's the place to look.

## Production (HTTPS on the droplet)

Use the Compose stack — it adds **Caddy** (automatic Let's Encrypt HTTPS) and resource caps:

```bash
cp .env.example .env     # set QUOTE_DOMAIN + ALLOWED_ORIGIN
docker compose up -d --build
```

Step-by-step (droplet, DNS, firewall, verify) in **[DEPLOY.md](DEPLOY.md)**.

## Wiring to the front-end

In [../prototype.html](../prototype.html), replace the in-browser `parseSTL`/`analyse` with a
`fetch('<service>/analyze', {method:'POST', body: formData})` and use the returned
geometry in `computePart`. When `units_known` is true (STEP), hide/lock the units selector.
Keep everything else (pricing, basket, delivery, quote) unchanged.

## 2b — FDM time & mass (scaffolded, UNTESTED)

`POST /slice` (multipart: `file`, `process`, `material`, `infill`) runs **OrcaSlicer**
(FDM only) and returns measured time + filament mass:

```json
{ "sliced": true, "print_time_h": 4.32, "filament_g": 85.7, "support_g": null, "message": "" }
```

- **Fail-soft:** if the slicer is missing / times out / unparseable it returns
  `{"sliced": false, "message": "...; using estimate."}`, and the front-end keeps its
  heuristic — nothing breaks. SLA/SLS also return `sliced:false` (priced on volume + bbox).
- Needs the **OrcaSlicer image** (`Dockerfile.slicer`) and **H2S profiles** (`profiles/`, see its README).
  Switch compose to `dockerfile: Dockerfile.slicer`, then `docker compose up -d --build`.
- **Front-end wiring (next):** for FDM parts, call `/slice` and on `sliced:true` use
  `print_time_h` + `filament_g` instead of the heuristic — same graceful-fallback
  pattern as the STEP `/analyze` wiring.
- ⚠ The CLI flags, the `sparse_infill_density` override, the g-code parsing in
  `slicer.py`, and the whole OrcaSlicer install in `Dockerfile.slicer` are all marked
  `VERIFY` — expect to tune them on the first real slice.

## Not in 2a/2b yet (later milestones — see ../PHASE2-SERVER.md)

- **2b** OrcaSlicer (FDM) → real print time + filament/support mass (pricing moves server-side)
- **2c** thin-wall detection + viewer highlighting
- **2d** WooCommerce checkout + manual-quote submission
- **2e** multi-body auto-split, result caching
- **Ops/security:** job queue (Redis/RQ), sandboxed workers with timeout + memory caps,
  ClamAV upload scan, signed URLs, rate limiting. **File retention:** delete CAD after
  fulfilment (keep order metadata only). Lock CORS to the WordPress origin.

## Files

- `app.py` — FastAPI endpoints
- `geometry.py` — STL (trimesh) + STEP (OpenCascade) analysis
- `environment.yml` — conda-forge dependencies (pinned)
- `Dockerfile` — micromamba image
