# OrcaSlicer profiles for the H2S (milestone 2b)

The slicer needs real OrcaSlicer config to produce accurate time/mass. **Export
these from OrcaSlicer (or Bambu Studio) for your H2S setup and drop them here** —
they're not in the repo because they're machine-specific and you'll tune them.

Expected files (paths overridable via env — see `../slicer.py`):

| File | What | Env var |
|---|---|---|
| `h2s_printer.json` | H2S machine/printer profile | `ORCA_PRINTER_JSON` |
| `h2s_process.json` | base process (layer height, supports, speeds) | `ORCA_PROCESS_JSON` |
| `filament/PLA.json`, `filament/ABS.json`, … | per-material filament profiles | `ORCA_FILAMENT_DIR` (+ `ORCA_FILAMENT_JSON` fallback) |

How to get them:

1. In OrcaSlicer, select the **H2S** printer (if it's not built in yet, import/clone the
   nearest Bambu profile and set the build volume to **340 × 320 × 340 mm**).
2. Set your standard process (layer height, supports policy) and each filament.
3. Export presets to JSON (OrcaSlicer stores user presets as JSON under its config
   dir; or use *File → Export → Export preset*). Copy them here with the names above.

Notes:

- The slicer overrides **infill per request** by setting `sparse_infill_density` on a
  copy of `h2s_process.json` — VERIFY that key name matches your profile; if not,
  keep one process profile per infill level instead and adjust `slicer.py`.
- One **filament profile per material** gives correct mass (density) and time (temps).
  Name them by the material key the front-end sends (PLA, ABS, ASA, PETG, CASTING,
  NYLON, PAHTCF). A missing one falls back to `ORCA_FILAMENT_JSON`.
- SLA/SLS don't use this — they're priced on volume + bounding box.
