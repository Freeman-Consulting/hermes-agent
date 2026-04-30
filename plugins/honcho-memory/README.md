# Honcho Memory Dashboard Plugin

Dashboard visibility into Honcho AI-native memory using the Hermes Plugin SDK.

## Features

- **Health check** — connectivity status and config info
- **Stats overview** — peer count, message totals, queue status
- **Peers list** — all peers with their card facts
- **Semantic search** — query memory by meaning
- **Config view** — current Honcho configuration (secrets redacted)

## Architecture

Calls the Honcho HTTP API directly (v3) — no SDK dependency required.
Reads config from `~/.hermes/honcho.json` for base_url.

Mounted at `/api/plugins/honcho-memory/` by the dashboard plugin system.

## Installation

Plugin lives at: `~/.hermes/plugins/honcho-memory/dashboard/`

To verify it's loaded:
1. Open the Hermes dashboard
2. Look for "Honcho Memory" tab in the sidebar
3. Or check `/api/dashboard/plugins` endpoint

## API Endpoints

- `GET /health` — connectivity and config status
- `GET /stats` — memory statistics (cached 30s)
- `GET /peers` — list all peers with cards
- `GET /peer/{peer_id}` — detailed peer info
- `POST /search` — semantic search (`{"query": "...", "limit": 10}`)
- `GET /conclusions` — list documents/conclusions
- `GET /config` — configuration snapshot

## Development

Backend: `dashboard/plugin_api.py` (FastAPI)
Frontend: `dashboard/dist/index.js` (Hermes Plugin SDK)
