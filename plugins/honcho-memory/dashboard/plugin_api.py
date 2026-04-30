"""Honcho Memory Dashboard Plugin — Backend API.

Calls the Honcho HTTP API directly (v3, no SDK required).
Reads config from ~/.hermes/honcho.json for base_url.

Mounted at /api/plugins/honcho-memory/ by Hermes dashboard.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from fastapi import APIRouter, HTTPException, Request
    from httpx import AsyncClient
except ImportError:
    class APIRouter:
        def get(self, *_a, **_kw): return lambda fn: fn
        def post(self, *_a, **_kw): return lambda fn: fn
    class HTTPException(Exception): pass
    class Request: pass

logger = logging.getLogger(__name__)
router = APIRouter()

# Cache TTL for stats (seconds)
_STATS_TTL = 30
_stats_cache: Dict[str, Any] = {"data": None, "ts": 0}

# Workspace ID from config (default to "hermes")
_WORKSPACE_ID = "hermes"


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def _load_honcho_config() -> Optional[Dict[str, Any]]:
    """Load Honcho config from the Hermes-resolved path."""
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    paths = [
        hermes_home / "honcho.json",
        Path.home() / ".hermes" / "honcho.json",
        Path.home() / ".honcho" / "config.json",
    ]
    
    for p in paths:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception as e:
                logger.debug("Failed to read Honcho config %s: %s", p, e)
    
    return None


def _get_api_base() -> Optional[str]:
    """Get the Honcho API base URL."""
    cfg = _load_honcho_config()
    if not cfg:
        return None
    
    base = cfg.get("base_url") or cfg.get("baseUrl") or os.environ.get("HONCHO_BASE_URL", "").strip()
    if base:
        return base.rstrip("/")
    
    # If we have an API key, use the cloud endpoint
    api_key = cfg.get("apiKey") or os.environ.get("HONCHO_API_KEY")
    if api_key:
        return "https://api.honcho.dev"
    
    return None


def _get_api_key() -> Optional[str]:
    """Get the Honcho API key."""
    cfg = _load_honcho_config()
    if not cfg:
        return None
    return cfg.get("apiKey") or os.environ.get("HONCHO_API_KEY")


# ---------------------------------------------------------------------------
# HTTP client helper
# ---------------------------------------------------------------------------

async def _honcho_request(method: str, path: str, **kwargs) -> Any:
    """Make a request to the Honcho API."""
    base = _get_api_base()
    api_key = _get_api_key()
    
    if not base:
        raise ValueError("Honcho base_url not configured")
    
    url = f"{base}{path}"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    try:
        async with AsyncClient(timeout=10) as client:
            response = await client.request(
                method, url, headers=headers, **kwargs
            )
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.debug("Honcho API request failed: %s %s -> %s", method, path, e)
        raise


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/health")
async def health():
    """Check Honcho connectivity and config status."""
    base = _get_api_base()
    api_key = _get_api_key()
    cfg = _load_honcho_config()
    
    config_info = {
        "enabled": bool(base or api_key),
        "has_api_key": bool(api_key),
        "has_base_url": bool(base),
    }
    
    if not base:
        return {
            "status": "not_configured",
            "message": "Honcho base_url not configured",
            "config": config_info,
        }
    
    try:
        await _honcho_request("POST", "/v3/workspaces", json={"id": _WORKSPACE_ID})
        return {
            "status": "healthy",
            "message": f"Connected to {base}",
            "config": config_info,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Connection failed: {e}",
            "config": config_info,
        }


@router.get("/stats")
async def stats():
    """Get Honcho memory statistics."""
    global _stats_cache
    now = time.time()
    if _stats_cache["data"] and (now - _stats_cache["ts"]) < _STATS_TTL:
        return {**_stats_cache["data"], "cached": True, "cache_age": int(now - _stats_cache["ts"])}

    try:
        await _honcho_request("POST", "/v3/workspaces", json={"id": _WORKSPACE_ID})
        peers_resp = await _honcho_request("POST", f"/v3/workspaces/{_WORKSPACE_ID}/peers/list", json={})
        peers = peers_resp.get("items", []) if isinstance(peers_resp, dict) else []
        
        queue = {}
        try:
            queue = await _honcho_request("GET", f"/v3/workspaces/{_WORKSPACE_ID}/queue/status")
        except Exception:
            pass
        
        payload = {
            "workspace_id": _WORKSPACE_ID,
            "peers_count": len(peers),
            "total_messages": peers_resp.get("total", 0),
            "queue": queue,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        
        _stats_cache = {"data": payload, "ts": now}
        return {**payload, "cached": False, "cache_age": 0}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stats error: {e}")


@router.get("/peers")
async def peers():
    """List all peers with their cards."""
    try:
        peers_resp = await _honcho_request("POST", f"/v3/workspaces/{_WORKSPACE_ID}/peers/list", json={})
        peers = peers_resp.get("items", []) if isinstance(peers_resp, dict) else []
        
        peer_list = []
        for peer in peers:
            peer_id = peer.get("id")
            try:
                card_resp = await _honcho_request("GET", f"/v3/workspaces/{_WORKSPACE_ID}/peers/{peer_id}/card")
                card = card_resp.get("peer_card", []) if isinstance(card_resp, dict) else []
                
                peer_list.append({
                    "id": peer_id,
                    "card": [{"fact": str(f)} for f in card] if card else [],
                    "created_at": peer.get("created_at"),
                    "metadata": peer.get("metadata", {}),
                    "configuration": peer.get("configuration", {}),
                })
            except Exception as e:
                logger.debug("Error getting peer %s card: %s", peer_id, e)
                peer_list.append({
                    "id": peer_id,
                    "card": [],
                    "created_at": peer.get("created_at"),
                    "metadata": {},
                    "configuration": {},
                })
        
        return {"peers": peer_list, "count": len(peer_list)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Peers error: {e}")


@router.get("/peer/{peer_id}")
async def get_peer(peer_id: str):
    """Get full details about a specific peer."""
    try:
        card_resp = await _honcho_request("GET", f"/v3/workspaces/{_WORKSPACE_ID}/peers/{peer_id}/card")
        card = card_resp.get("peer_card", []) if isinstance(card_resp, dict) else []
        
        # Get peer context for more details
        context_resp = await _honcho_request("GET", f"/v3/workspaces/{_WORKSPACE_ID}/peers/{peer_id}/context")
        representation = context_resp.get("representation", "") if isinstance(context_resp, dict) else ""
        
        # Get recent messages for this peer
        messages = []
        try:
            msg_resp = await _honcho_request("POST", f"/v3/workspaces/{_WORKSPACE_ID}/peers/{peer_id}/messages/list", json={"filters": {"limit": 20}})
            if isinstance(msg_resp, dict) and "items" in msg_resp:
                messages = msg_resp["items"][:20]
        except Exception:
            pass
        
        return {
            "id": peer_id,
            "card": [{"fact": str(f)} for f in card] if card else [],
            "representation": representation,
            "messages": messages,
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Peer not found: {e}")


@router.post("/search")
async def search(body: dict):
    """Semantic search across Honcho memory.
    
    Expects: {"query": "...", "limit": 10}
    """
    query = body.get("query", "")
    limit = int(body.get("limit", 10))
    
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")

    try:
        results_resp = await _honcho_request(
            "POST",
            f"/v3/workspaces/{_WORKSPACE_ID}/search",
            json={"query": query, "limit": min(limit, 100)}
        )
        
        return {
            "query": query,
            "results": results_resp if isinstance(results_resp, list) else [],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search error: {e}")


@router.get("/conclusions")
async def conclusions():
    """List conclusions (documents) in the workspace."""
    try:
        resp = await _honcho_request("POST", f"/v3/workspaces/{_WORKSPACE_ID}/conclusions/list", json={})
        items = resp.get("items", []) if isinstance(resp, dict) else []
        
        return {
            "conclusions": [{"id": c.get("id"), "content": c.get("content"), "observer_id": c.get("observer_id"), "observed_id": c.get("observed_id")} for c in items],
            "total": resp.get("total", 0),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Conclusions error: {e}")


@router.get("/config")
async def config():
    """Get Honcho configuration (secrets redacted)."""
    cfg = _load_honcho_config()
    
    if not cfg:
        return {"status": "not_configured"}
    
    safe_cfg = {k: v for k, v in cfg.items() if k not in ("apiKey", "api_key", "hosts")}
    
    hosts = cfg.get("hosts", {})
    if hosts:
        safe_hosts = {}
        for host, settings in hosts.items():
            if isinstance(settings, dict):
                safe_hosts[host] = {k: v for k, v in settings.items() if k not in ("apiKey",)}
            else:
                safe_hosts[host] = settings
        safe_cfg["hosts"] = safe_hosts
    
    return {
        "status": "configured",
        "config": safe_cfg,
    }
