# Deploying the geometry service (DigitalOcean droplet)

A small, stateless HTTPS service. Runs on its **own droplet** — *not* the MRP box
(see [../PHASE2-SERVER.md](../PHASE2-SERVER.md) §12.1). Stateless = nothing to back up.

## 1. Droplet

- Create a **Ubuntu 22.04/24.04** droplet, **2–4 GB RAM**, in the **same DigitalOcean project/VPC** as the MRP (private networking, but isolated).
- Firewall (DO cloud firewall or `ufw`): allow **22** (SSH), **80**, **443** only.
  ```bash
  ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
  ```

## 2. Docker

```bash
curl -fsSL https://get.docker.com | sh        # installs Docker + compose plugin
```

## 3. DNS

Point an A record at the droplet's public IP:

```
quote-api.s-can.co.uk  →  <droplet-ip>
```

(Caddy gets a Let's Encrypt cert automatically once this resolves.)

## 4. Code + config

```bash
# copy the server/ directory to the droplet (git clone, or scp -r server/ ...)
cd server
cp .env.example .env
nano .env          # set QUOTE_DOMAIN=quote-api.s-can.co.uk and ALLOWED_ORIGIN=https://s-can.co.uk
```

## 5. Run

```bash
docker compose up -d --build      # first build pulls OpenCascade — a few minutes
docker compose ps                 # geometry should be "healthy"
docker compose logs -f geometry   # watch for errors
```

## 6. Verify

```bash
curl https://quote-api.s-can.co.uk/health
# {"ok":true,"service":"scan-geometry","version":"0.1.0"}

curl -F "file=@part.step" https://quote-api.s-can.co.uk/analyze
curl -F "file=@part.stl"  https://quote-api.s-can.co.uk/analyze
```

## 7. Connect the front-end

On the page that hosts the quote tool (WordPress, or the prototype for testing), set:

```html
<script>window.SCAN_API = "https://quote-api.s-can.co.uk";</script>
```

or test the prototype directly with `prototype.html?api=https://quote-api.s-can.co.uk`.
STEP uploads now price from real OpenCascade geometry; STL stays in-browser.

## Operations

- **Update:** `git pull` (or re-copy) → `docker compose up -d --build`.
- **Logs:** `docker compose logs -f`.
- **Restart policy:** `unless-stopped` — survives reboots.
- **Resource caps:** geometry is limited to 2 GB / 1.5 CPU so a pathological upload can't starve the box.
- **No data to retain:** the service holds no CAD files (processed in-memory / temp, then gone). Order history lives in WooCommerce.
- **Security:** CORS locked to `ALLOWED_ORIGIN`; the service isn't exposed directly (only via Caddy on 443). Later milestones add an upload AV scan + a job queue with sandboxed, time-limited workers (see ../PHASE2-SERVER.md §8).
