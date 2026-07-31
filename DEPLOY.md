# Running agreementatlas.com from this Mac

The public site is this Mac, reached through a **Cloudflare Tunnel** — an
outbound connection from your machine to Cloudflare, so visitors reach
Cloudflare and Cloudflare reaches you. Nothing listens on your router and no
port is forwarded.

**Two servers run side by side, on purpose:**

| port | mode | `/api/families` | what it is |
|---|---|---|---|
| **8000** | local | 200, lists all 16 | your development server, with `data/library` |
| **8001** | public-demo | **only sample families**, every entry `"is_sample": true` | what the world sees |

`data/library/` holds licensed third-party agreements including the OpenText
PDFs. **The tunnel points at 8001, never 8000.** In public-demo mode there is
no shared library: each visitor session carries its own family list — the two
published sample bundles, pre-enriched and read-only, plus up to three families
the visitor creates — and all of it expires together after 6 hours.

---

## First-time setup

1. **Log in to Cloudflare.** This opens a browser — pick the
   **agreementatlas.com** zone when asked.

       cloudflared tunnel login

2. **Create the tunnel and DNS records:**

       scripts/setup_tunnel.sh

3. **Install both background services:**

       launchctl load ~/Library/LaunchAgents/com.agreementatlas.app.plist
       launchctl load ~/Library/LaunchAgents/com.agreementatlas.tunnel.plist

4. **Check it:** open <https://agreementatlas.com>

---

## Daily use

**Is it up?**

    launchctl list | grep agreementatlas
    curl -sI https://agreementatlas.com | head -1

Two lines means both services are loaded. The number in the second column is the
last exit code — `0` is healthy.

**Start / stop / restart**

    # stop
    launchctl unload ~/Library/LaunchAgents/com.agreementatlas.app.plist
    launchctl unload ~/Library/LaunchAgents/com.agreementatlas.tunnel.plist

    # start
    launchctl load ~/Library/LaunchAgents/com.agreementatlas.app.plist
    launchctl load ~/Library/LaunchAgents/com.agreementatlas.tunnel.plist

    # restart just the app after a code change
    launchctl kickstart -k gui/$(id -u)/com.agreementatlas.app

**Logs**

    tail -f ~/Library/Logs/agreementatlas-app.log
    tail -f ~/Library/Logs/agreementatlas-tunnel.log

**Run by hand instead** (useful when debugging — Ctrl-C to stop):

    scripts/serve_public.sh                  # terminal 1
    cloudflared tunnel run agreementatlas    # terminal 2

---

## The site loads but every question fails

The tunnel and the app are independent of **LM Studio**, which does the actual
inference. If LM Studio is not running, pages render and queries error.

1. Open **LM Studio**.
2. Go to the **Developer** tab and make sure the server is **Running** on port
   **1234**.
3. Confirm both models are loaded:
   - `google/gemma-4-26b-a4b-qat`
   - `text-embedding-nomic-embed-text-v1.5`
4. Check from the terminal:

       curl -s http://127.0.0.1:1234/v1/models | head -c 300

**The Mac must also be awake.** Sleep kills the site. Before you send the link
to anyone:

- **System Settings → Lock Screen → Turn display off on power adapter: Never**
- or keep it awake for a few hours with: `caffeinate -dims -t 14400`

---

## Safety checks — run these after any change

    # MUST print "OK: samples only". A fresh visitor may see only the shipped
    # sample families. If this fails — any '"is_sample": false' entry, or the
    # 16-family local library — the tunnel is pointed at the wrong port and
    # licensed agreements are public. Stop the tunnel at once.
    curl -s https://agreementatlas.com/api/families | python3 -c \
      'import json,sys; f=json.load(sys.stdin)["families"]; \
       assert f and all(x["is_sample"] for x in f) and len(f) <= 4, f; print("OK: samples only")'

    # MUST be 200
    curl -s -o /dev/null -w "%{http_code}\n" https://agreementatlas.com/

---

## Rollback — take the site down now

    launchctl unload ~/Library/LaunchAgents/com.agreementatlas.tunnel.plist

The domain stops resolving to this machine within seconds. To remove it
permanently:

    cloudflared tunnel delete agreementatlas
    rm ~/.cloudflared/config.yml

---

## What lives where

| thing | path | in git? |
|---|---|---|
| public server script | `scripts/serve_public.sh` | yes |
| tunnel setup script | `scripts/setup_tunnel.sh` | yes |
| tunnel config | `~/.cloudflared/config.yml` | no |
| Cloudflare cert | `~/.cloudflared/cert.pem` | no — **secret** |
| tunnel credentials | `~/.cloudflared/<uuid>.json` | no — **secret** |
| launchd services | `~/Library/LaunchAgents/com.agreementatlas.*.plist` | no |
| operator name / email | `.env` | no — gitignored |

Nothing secret is inside the repository. The two scripts read operator details
from `.env`, which is gitignored.
