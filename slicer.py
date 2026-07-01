"""
OrcaSlicer-based time / filament-mass for FDM  (Phase 2, milestone 2b).

⚠ UNTESTED — authored without a runnable slicer. OrcaSlicer's CLI flags, the
profile/override mechanism and the g-code output format are all version-sensitive.
Every block marked `VERIFY` must be checked against your OrcaSlicer build + the
real H2S profile on first run.

Design: FAIL SOFT. If the slicer is missing, times out, or anything can't be
parsed, slice_fdm() returns {"sliced": False, "message": ...} and the caller
falls back to the volume heuristic — so the service stays usable while 2b is tuned.

Env config (set in the Dockerfile / compose):
  ORCA_BIN             OrcaSlicer binary           (default: /opt/orca/AppRun)
  ORCA_PRINTER_JSON    H2S machine profile         (default: /app/profiles/h2s_printer.json)
  ORCA_PROCESS_JSON    base process profile        (default: /app/profiles/h2s_process.json)
  ORCA_FILAMENT_DIR    per-material filament dir    (default: /app/profiles/filament)
  ORCA_FILAMENT_JSON   fallback filament profile    (default: .../filament/PLA.json)
  ORCA_TIMEOUT_S       hard per-slice timeout (s)   (default: 180)
"""

import os
import re
import shutil
import json
import zipfile
import subprocess
import tempfile

ORCA_BIN = os.getenv("ORCA_BIN", "/opt/orca/AppRun")
PRINTER = os.getenv("ORCA_PRINTER_JSON", "/app/profiles/h2s_printer.json")
PROCESS = os.getenv("ORCA_PROCESS_JSON", "/app/profiles/h2s_process.json")
FILAMENT_DIR = os.getenv("ORCA_FILAMENT_DIR", "/app/profiles/filament")
DEFAULT_FILAMENT = os.getenv("ORCA_FILAMENT_JSON", "/app/profiles/filament/PLA.json")
TIMEOUT = int(os.getenv("ORCA_TIMEOUT_S", "180"))


def available() -> bool:
    return bool(shutil.which(ORCA_BIN) or os.path.exists(ORCA_BIN))


def _filament_for(material: str) -> str:
    p = os.path.join(FILAMENT_DIR, (material or "PLA").upper() + ".json")
    return p if os.path.exists(p) else DEFAULT_FILAMENT


_TIME_RE = re.compile(r"estimated printing time[^=]*=\s*(.+)", re.I)
_FIL_G_RE = re.compile(r"(?:total\s+)?filament used\s*\[g\]\s*=\s*([\d.]+)", re.I)


def _parse_hms(s: str) -> float:
    """'1h 23m 45s' / '23m 5s' / '45s' -> hours."""
    h = m = sec = 0
    for val, unit in re.findall(r"(\d+)\s*([hms])", s):
        v = int(val)
        if unit == "h":
            h = v
        elif unit == "m":
            m = v
        else:
            sec = v
    return h + m / 60.0 + sec / 3600.0


def _log(msg):
    # Plain stdout print — picked up by `docker compose logs -f geometry` immediately.
    # The /slice endpoint always returns HTTP 200 even on a soft failure (by design —
    # see app.py), so the access log alone never shows WHY a slice didn't produce a
    # result. This is the only place that reason is visible.
    print("[slice] " + msg, flush=True)


def slice_fdm(stl_bytes: bytes, infill_pct: int = 20, material: str = "PLA") -> dict:
    if not available():
        _log("ORCA_BIN not found at %s — slicer not installed; using estimate." % ORCA_BIN)
        return {"sliced": False, "message": "Slicer not installed; using estimate."}

    work = tempfile.mkdtemp(prefix="orca_")
    try:
        stl_path = os.path.join(work, "part.stl")
        with open(stl_path, "wb") as f:
            f.write(stl_bytes)

        # VERIFY: per-request infill. Derive a process json with sparse_infill_density
        # set, from the base profile. If that profile key differs in your version,
        # adjust here (or keep one process profile per infill level instead).
        proc_path = PROCESS
        try:
            with open(PROCESS) as f:
                proc = json.load(f)
            proc["sparse_infill_density"] = "%d%%" % int(infill_pct)
            proc_path = os.path.join(work, "process.json")
            with open(proc_path, "w") as f:
                json.dump(proc, f)
        except Exception:
            pass  # fall back to the base process profile unchanged

        out_dir = os.path.join(work, "out")
        os.makedirs(out_dir, exist_ok=True)

        # VERIFY: flags for your OrcaSlicer version. `--mstpp` is the per-plate
        # timeout (ms) — keep it; without it a bad model can slice forever.
        cmd = [
            ORCA_BIN, "--slice", "0",
            "--load-settings", "%s;%s" % (PRINTER, proc_path),
            "--load-filaments", _filament_for(material),
            "--mstpp", str(TIMEOUT * 1000),
            "--outputdir", out_dir,
            "--export-gcode", stl_path,
        ]
        if shutil.which("xvfb-run"):   # headless safety (some builds need a display)
            cmd = ["xvfb-run", "-a"] + cmd

        _log("running: " + " ".join(cmd))
        proc_run = subprocess.run(cmd, timeout=TIMEOUT + 20, capture_output=True, check=False)
        _log("exit code %s" % proc_run.returncode)
        if proc_run.stdout:
            _log("stdout tail: " + proc_run.stdout[-500:].decode("latin-1", "ignore"))
        if proc_run.stderr:
            _log("stderr tail: " + proc_run.stderr[-500:].decode("latin-1", "ignore"))

        gpath = _find_gcode(out_dir)
        if not gpath:
            tail = (proc_run.stderr or b"")[-300:].decode("latin-1", "ignore")
            _log("no g-code found in %s" % out_dir)
            return {"sliced": False, "message": "Slice produced no g-code; using estimate. " + tail}

        head = _read_text(gpath)
        tmatch = _TIME_RE.search(head)
        gmatch = _FIL_G_RE.search(head)
        if not tmatch or not gmatch:
            _log("g-code found (%s) but time/mass regex didn't match — check the comment format." % gpath)
            _log("g-code head sample: " + head[:800])
            return {"sliced": False, "message": "Could not parse slice output; using estimate."}

        _log("OK — time=%s mass=%sg" % (tmatch.group(1), gmatch.group(1)))
        return {
            "sliced": True,
            "print_time_h": round(_parse_hms(tmatch.group(1)), 3),
            "filament_g": float(gmatch.group(1)),
            "support_g": None,   # VERIFY: parse separately if your profile reports it
            "message": "",
        }
    except subprocess.TimeoutExpired:
        _log("subprocess.run TIMED OUT after %ss" % (TIMEOUT + 20))
        return {"sliced": False, "message": "Slice timed out; using estimate."}
    except Exception as e:
        _log("EXCEPTION: %r" % e)
        return {"sliced": False, "message": "Slice failed (%s); using estimate." % e}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _find_gcode(d: str):
    for root, _, files in os.walk(d):
        for fn in files:
            if fn.endswith(".gcode") or fn.endswith(".gcode.3mf") or fn.endswith(".3mf"):
                return os.path.join(root, fn)
    return None


def _read_text(path: str) -> str:
    """Return head+tail text of the g-code (comments live in either). Handles .3mf zips."""
    if path.endswith(".3mf"):
        try:
            with zipfile.ZipFile(path) as z:
                for nm in z.namelist():
                    if nm.lower().endswith(".gcode"):
                        t = z.read(nm).decode("latin-1", "ignore")
                        return t[:8000] + "\n" + t[-8000:]
        except Exception:
            return ""
        return ""
    with open(path, "rb") as f:
        t = f.read().decode("latin-1", "ignore")
    return t[:8000] + "\n" + t[-8000:]
