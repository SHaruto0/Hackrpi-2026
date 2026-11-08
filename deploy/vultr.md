# Deploy Firesight on Vultr

1. Create an Ubuntu 24.04 cloud instance (2 vCPU, 4 GB; 8 GB if you build with VPS=1). Note the public IPv4 address.
2. In the Vultr firewall, allow TCP 22, 80, and 443. SSH in as root.
3. Install Docker Engine and the Compose plugin: https://docs.docker.com/engine/install/ubuntu/
4. Clone this repo onto the instance and `cd` into it.
5. Create a `.env` file (do not commit it). Set `SITE` to the public IP with dots changed to dashes plus `.sslip.io` (example: `1-2-3-4.sslip.io`).
6. In that same `.env`, set only the keys you use: `FIRESIGHT_BUILDING` (default `data/buildings/dcc.json`), `FISH_API_KEY`, `FISH_MODEL`, `FISH_VOICE_ID`, `MONGODB_URI`, `MONGODB_DB`, `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_PAT`, `SNOWFLAKE_MODEL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_API_VERSION`, `GEMINI_EDIT_MODEL`. Leave unused keys unset.
7. For camera positioning, put the model at `data/vps/final_model` (already in the repo) and set `VPS=1` in `.env`. Leave `VPS` unset for the light image.
8. Start it: `docker compose up -d --build`. Caddy requests a certificate for `$SITE` and proxies HTTPS to the app on port 8000.
9. Check `https://$SITE` (3D viewer) and `https://$SITE/phone.html` (occupant). Phones need this HTTPS host for camera and compass.
10. After a map or code change, run `docker compose up -d --build` again. To rotate a key, edit `.env` and run `docker compose up -d`.
