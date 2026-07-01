"""
Geometry analysis for the S-CAN quoting service (Phase 2, milestone 2a).

Returns the same fields the Phase-1 front-end computes in the browser, but
server-side and for STEP as well as STL:

    volume_cm3, area_cm2, bbox_mm{x,y,z}, watertight, bodies, units_known, valid, message

Notes
-----
* STL is unitless -> reported in its native numbers; the client applies the
  mm/cm/inch selector + scale. `units_known` is False.
* STEP carries real units -> forced to MILLIMETRES on read, so the client can
  lock units to mm. `units_known` is True.
* No pricing here. The client keeps the config-driven price (Phase 1); pricing
  moves server-side in milestone 2b when slicer-measured FDM time is authoritative.

Tested against: trimesh >= 4, pythonocc-core 7.8.x (conda-forge). The OCC API
names below are the 7.8 form (`brepgprop.VolumeProperties`, `brepbndlib.AddOptimal`);
earlier 7.x used the `brepgprop_VolumeProperties` free-function form.
"""

import io
import os
import tempfile


def analyze(filename: str, data: bytes) -> dict:
    """Dispatch on extension. Never raises — returns {'valid': False, 'message': ...} on failure."""
    name = (filename or "").lower()
    try:
        if name.endswith(".step") or name.endswith(".stp"):
            return analyze_step(data)
        if name.endswith(".stl"):
            return analyze_stl(data)
        return {"valid": False, "message": "Unsupported file type — STL or STEP only."}
    except Exception as e:  # never leak a stack trace to the client
        return {"valid": False, "message": "Could not analyse the file: %s" % e}


# ---------------------------------------------------------------- STL (trimesh)
def analyze_stl(data: bytes) -> dict:
    import trimesh

    mesh = trimesh.load(io.BytesIO(data), file_type="stl")
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.dump(concatenate=True)
    if mesh is None or getattr(mesh, "faces", None) is None or len(mesh.faces) == 0:
        return {"valid": False, "message": "No triangles found in the STL."}

    ext = mesh.extents  # mm (assumed) [x, y, z]
    try:
        bodies = len(mesh.split(only_watertight=False))
    except Exception:
        bodies = 1
    return {
        "valid": True,
        "format": "STL",
        "units_known": False,            # client applies mm/cm/inch + scale
        "volume_cm3": abs(float(mesh.volume)) / 1000.0,
        "area_cm2": float(mesh.area) / 100.0,
        "bbox_mm": {"x": float(ext[0]), "y": float(ext[1]), "z": float(ext[2])},
        "bodies": int(bodies) if bodies else 1,
        "watertight": bool(mesh.is_watertight),
        "triangles": int(len(mesh.faces)),
        "message": "" if mesh.is_watertight else "Mesh is not watertight — volume may be unreliable.",
    }


# --------------------------------------------------------------- STEP (OpenCascade)
def analyze_step(data: bytes) -> dict:
    from OCC.Core.STEPControl import STEPControl_Reader
    from OCC.Core.IFSelect import IFSelect_RetDone
    from OCC.Core.Interface import Interface_Static
    from OCC.Core.GProp import GProp_GProps
    from OCC.Core.BRepGProp import brepgprop
    from OCC.Core.Bnd import Bnd_Box
    from OCC.Core.BRepBndLib import brepbndlib
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopAbs import TopAbs_SOLID
    from OCC.Core.TopoDS import topods
    from OCC.Core.BRepCheck import BRepCheck_Analyzer

    # Force the importer to convert to millimetres regardless of the file's unit.
    Interface_Static.SetCVal("xstep.cascade.unit", "MM")

    tmp = tempfile.NamedTemporaryFile(suffix=".step", delete=False)
    try:
        tmp.write(data)
        tmp.close()

        reader = STEPControl_Reader()
        if reader.ReadFile(tmp.name) != IFSelect_RetDone:
            return {"valid": False, "message": "Could not read the STEP file."}
        reader.TransferRoots()
        shape = reader.OneShape()

        solids = []
        exp = TopExp_Explorer(shape, TopAbs_SOLID)
        while exp.More():
            solids.append(topods.Solid(exp.Current()))
            exp.Next()
        if not solids:
            return {"valid": False,
                    "message": "STEP file has no closed solid (surfaces only) — submit for review."}

        volume_mm3 = 0.0
        area_mm2 = 0.0
        all_valid = True
        per_solid = []   # one entry per solid, in TopExp_Explorer order — this order is the
                          # contract with tessellate_step_to_stl()'s body_index parameter, so
                          # the client can later ask for "just body i" and get the same one.
        for s in solids:
            vp = GProp_GProps()
            brepgprop.VolumeProperties(s, vp)
            sp = GProp_GProps()
            brepgprop.SurfaceProperties(s, sp)
            s_valid = BRepCheck_Analyzer(s).IsValid()
            volume_mm3 += vp.Mass()
            area_mm2 += sp.Mass()
            all_valid = all_valid and s_valid

            sbox = Bnd_Box()
            try:
                brepbndlib.AddOptimal(s, sbox)
            except Exception:
                brepbndlib.Add(s, sbox)
            sx0, sy0, sz0, sx1, sy1, sz1 = sbox.Get()
            per_solid.append({
                "volume_cm3": abs(vp.Mass()) / 1000.0,
                "area_cm2": sp.Mass() / 100.0,
                "bbox_mm": {"x": sx1 - sx0, "y": sy1 - sy0, "z": sz1 - sz0},
                "watertight": bool(s_valid),
            })

        box = Bnd_Box()
        try:
            brepbndlib.AddOptimal(shape, box)
        except Exception:
            brepbndlib.Add(shape, box)
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()

        return {
            "valid": True,
            "format": "STEP",
            "units_known": True,            # resolved to mm
            "volume_cm3": abs(volume_mm3) / 1000.0,
            "area_cm2": area_mm2 / 100.0,
            "bbox_mm": {"x": xmax - xmin, "y": ymax - ymin, "z": zmax - zmin},
            "bodies": len(solids),
            "watertight": bool(all_valid),
            "triangles": None,
            "message": "" if all_valid else "Kernel reports an invalid solid — review advised.",
            # Only set when there's more than one solid — the client splits into separate
            # priced lines in that case, using each entry's own numbers (not the aggregate
            # above). Single-solid files keep the exact same response shape as before.
            "bodies_detail": per_solid if len(solids) > 1 else None,
        }
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def scale_stl(stl_bytes: bytes, factor: float) -> bytes:
    """Uniformly scale an STL (so the slicer sees the part at its final size)."""
    if abs(factor - 1.0) < 1e-9:
        return stl_bytes
    import trimesh
    m = trimesh.load(io.BytesIO(stl_bytes), file_type="stl")
    if isinstance(m, trimesh.Scene):
        m = m.dump(concatenate=True)
    m.apply_scale(float(factor))
    return m.export(file_type="stl")


def thin_wall_analysis(stl_bytes: bytes, min_wall_mm: float, sample_cap: int = 3000) -> dict:
    """Approximate local wall thickness via inward ray casting from sampled surface points.

    `stl_bytes` must already be at the part's FINAL printed size (the caller tessellates
    STEP and applies scale_stl() first, exactly like the /slice flow) — thickness in mm
    only means something once the geometry is at real size.

    Method: for a sample of face centroids, cast a ray inward along the negative face
    normal and measure the distance to the opposite wall. This is a standard, cheap
    approximation (used by most 3D-print preflight tools) — NOT exact, and known to be
    noisy right at edges/corners where rays graze rather than cross cleanly. That's why
    the verdict below tolerates a small percentage of below-threshold hits rather than
    rejecting on a single low sample. See PHASE2-SERVER.md §5.3 for the honest caveats.

    Always returns a dict with an `ok` flag; never raises. The caller (app.py) should
    treat `ok: False` as "skip this check" — a measurement failure must never be treated
    as a hard reject, only a successful `verdict: "review"` should be.
    """
    import numpy as np
    import trimesh
    try:
        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl")
        if isinstance(mesh, trimesh.Scene):
            mesh = mesh.dump(concatenate=True)
        if mesh is None or len(mesh.faces) == 0:
            return {"ok": False, "message": "No geometry to sample."}
        if not mesh.is_watertight:
            # An open mesh lets rays leak through the gap, producing nonsense distances —
            # better to skip than report a misleading number. The existing watertight
            # check already routes this part to review for a different reason.
            return {"ok": False, "message": "Mesh is not watertight — thickness needs a closed solid."}

        n_faces = len(mesh.faces)
        idx = (np.random.default_rng(0).choice(n_faces, size=sample_cap, replace=False)
               if n_faces > sample_cap else np.arange(n_faces))

        origins = mesh.triangles_center[idx]
        normals = mesh.face_normals[idx]
        eps = max(float(mesh.extents.max()) * 1e-4, 1e-4)   # nudge off the surface so the ray doesn't immediately re-hit its own face
        ray_origins = origins - normals * eps
        ray_dirs = -normals

        locations, index_ray, _ = mesh.ray.intersects_location(
            ray_origins=ray_origins, ray_directions=ray_dirs, multiple_hits=False
        )
        if len(index_ray) == 0:
            return {"ok": False, "message": "Ray casting found no interior hits."}

        dists = np.linalg.norm(locations - ray_origins[index_ray], axis=1)
        min_thick = float(dists.min())
        pct_below = float((dists < min_wall_mm).sum()) / float(len(dists)) * 100.0
        # Tolerate a small percentage below threshold — grazing-angle samples near edges
        # are a known false-positive source for this method; a lone outlier shouldn't
        # bounce an otherwise-fine part to manual review.
        verdict = "review" if pct_below > 2.0 else "pass"

        return {
            "ok": True,
            "min_thickness_mm": round(min_thick, 3),
            "pct_below_threshold": round(pct_below, 1),
            "min_wall_mm": min_wall_mm,
            "samples": int(len(dists)),
            "verdict": verdict,
        }
    except Exception as e:
        return {"ok": False, "message": "Thickness analysis failed: %s" % e}


def tessellate_step_to_stl(data: bytes, deflection: float = 0.1, body_index: int = None) -> bytes:
    """STEP -> tessellated binary STL bytes, for feeding the FDM slicer (milestone 2b),
    the /preview viewer, or /thinwall. If `body_index` is given, tessellate only that one
    solid (0-based, in the same TopExp_Explorer order analyze_step() reports bodies_detail
    in) — used when the client has split a multi-body file into separate priced parts and
    wants the mesh for just one of them."""
    from OCC.Core.STEPControl import STEPControl_Reader
    from OCC.Core.IFSelect import IFSelect_RetDone
    from OCC.Core.Interface import Interface_Static
    from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
    from OCC.Core.StlAPI import StlAPI_Writer
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopAbs import TopAbs_SOLID
    from OCC.Core.TopoDS import topods

    Interface_Static.SetCVal("xstep.cascade.unit", "MM")
    tin = tempfile.NamedTemporaryFile(suffix=".step", delete=False)
    tout = tempfile.NamedTemporaryFile(suffix=".stl", delete=False)
    tin.write(data)
    tin.close()
    tout.close()
    try:
        reader = STEPControl_Reader()
        if reader.ReadFile(tin.name) != IFSelect_RetDone:
            raise ValueError("Could not read STEP for tessellation.")
        reader.TransferRoots()
        shape = reader.OneShape()
        if body_index is not None:
            exp = TopExp_Explorer(shape, TopAbs_SOLID)
            solids = []
            while exp.More():
                solids.append(topods.Solid(exp.Current()))
                exp.Next()
            if body_index < 0 or body_index >= len(solids):
                raise ValueError("body_index %d out of range (0..%d)" % (body_index, len(solids) - 1))
            shape = solids[body_index]
        BRepMesh_IncrementalMesh(shape, deflection)
        writer = StlAPI_Writer()
        writer.SetASCIIMode(False)
        writer.Write(shape, tout.name)
        with open(tout.name, "rb") as f:
            return f.read()
    finally:
        for p in (tin.name, tout.name):
            try:
                os.unlink(p)
            except OSError:
                pass
