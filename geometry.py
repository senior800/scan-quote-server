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
        for s in solids:
            vp = GProp_GProps()
            brepgprop.VolumeProperties(s, vp)
            volume_mm3 += vp.Mass()
            sp = GProp_GProps()
            brepgprop.SurfaceProperties(s, sp)
            area_mm2 += sp.Mass()
            if not BRepCheck_Analyzer(s).IsValid():
                all_valid = False

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


def tessellate_step_to_stl(data: bytes, deflection: float = 0.1) -> bytes:
    """STEP -> tessellated binary STL bytes, for feeding the FDM slicer (milestone 2b)."""
    from OCC.Core.STEPControl import STEPControl_Reader
    from OCC.Core.IFSelect import IFSelect_RetDone
    from OCC.Core.Interface import Interface_Static
    from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
    from OCC.Core.StlAPI import StlAPI_Writer

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
