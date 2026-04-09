"""HubSpot integration — CRM card on Deal page + prefill Scriptius client card.

Setup: create a Private App in HubSpot with a CRM card pointing to this server.
When a deal page loads, HubSpot calls our endpoint → we fetch contact data → show card.
Sales rep clicks "Start Call" → data goes to Scriptius.
"""

import os
import json
import logging
import asyncio

import httpx
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger("scriptius.hubspot")

router = APIRouter(prefix="/api/hubspot", tags=["hubspot"])

HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
# Public URL of Scriptius server (for action buttons)
SCRIPTIUS_URL = os.getenv("SCRIPTIUS_URL", "http://localhost:8000")


# ── Shared state: latest prefill data for active session ────────────────────

_prefill_data: dict | None = None
_prefill_event: asyncio.Event = asyncio.Event()


def get_pending_prefill() -> dict | None:
    global _prefill_data
    data = _prefill_data
    _prefill_data = None
    _prefill_event.clear()
    return data


def set_prefill(data: dict) -> None:
    global _prefill_data
    _prefill_data = data
    _prefill_event.set()


# ── HubSpot API helpers ─────────────────────────────────────────────────────

async def _hs_get(path: str, token: str = "") -> dict:
    tk = token or HUBSPOT_TOKEN
    if not tk:
        return {}
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.hubapi.com{path}",
            headers={"Authorization": f"Bearer {tk}"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.error(f"HubSpot API {resp.status_code}: {resp.text[:200]}")
            return {}
        return resp.json()


async def fetch_deal_with_contact(deal_id: str) -> dict:
    """Fetch deal + first associated contact. Returns Scriptius fields."""
    deal = await _hs_get(
        f"/crm/v3/objects/deals/{deal_id}"
        f"?properties=dealname,amount&associations=contacts"
    )
    if not deal:
        return {}

    result = {}
    deal_props = deal.get("properties", {})
    if deal_props.get("dealname"):
        result["_dealName"] = deal_props["dealname"]

    # Get first associated contact
    assoc = deal.get("associations", {}).get("contacts", {}).get("results", [])
    if assoc:
        cid = str(assoc[0].get("id", ""))
        if cid:
            contact = await _hs_get(
                f"/crm/v3/objects/contacts/{cid}"
                f"?properties=firstname,lastname,phone,mobilephone,jobtitle,company,industry"
            )
            props = contact.get("properties", {})
            first = props.get("firstname", "") or ""
            last = props.get("lastname", "") or ""
            name = f"{first} {last}".strip()
            if name:
                result["name"] = name
            if props.get("jobtitle"):
                result["role"] = props["jobtitle"]
            if props.get("company"):
                result["company"] = props["company"]
            if props.get("industry"):
                result["industry"] = props["industry"]
            result["_phone"] = props.get("mobilephone") or props.get("phone") or ""

    return result


# ── CRM Card endpoint (called by HubSpot when deal page loads) ──────────────

@router.get("/card")
async def crm_card(
    hs_object_id: str = Query(default="", alias="hs_object_id"),
    associatedObjectId: str = Query(default=""),
    associatedObjectType: str = Query(default=""),
    portalId: str = Query(default=""),
    userId: str = Query(default=""),
):
    """HubSpot CRM Card data fetch endpoint.

    HubSpot calls this when a Deal page loads. We fetch the deal + contact data
    and return a card with client info and a "Start Call" action button.
    """
    deal_id = hs_object_id
    if not deal_id:
        return _empty_card("No deal ID")

    logger.info(f"CRM card requested for deal {deal_id} (portal {portalId})")

    # Fetch deal + contact from HubSpot
    data = await fetch_deal_with_contact(deal_id)
    if not data:
        return _empty_card("Could not fetch deal data")

    name = data.get("name", "Unknown")
    role = data.get("role", "—")
    company = data.get("company", "—")
    phone = data.get("_phone", "—")

    # Build action URL — clicking this will prefill Scriptius
    prefill_url = f"{SCRIPTIUS_URL}/api/hubspot/start-call?deal_id={deal_id}"

    return {
        "results": [
            {
                "objectId": deal_id,
                "title": name,
                "properties": [
                    {"label": "Role", "dataType": "STRING", "value": role},
                    {"label": "Company", "dataType": "STRING", "value": company},
                    {"label": "Phone", "dataType": "STRING", "value": phone},
                ],
                "actions": [
                    {
                        "type": "IFRAME",
                        "width": 890,
                        "height": 748,
                        "uri": prefill_url,
                        "label": "Start Call in Scriptius",
                    }
                ],
            }
        ],
    }


def _empty_card(reason: str) -> dict:
    return {
        "results": [
            {
                "objectId": 0,
                "title": "Scriptius",
                "properties": [
                    {"label": "Status", "dataType": "STRING", "value": reason},
                ],
            }
        ],
    }


# ── "Start Call" action (triggered by CRM card button click) ────────────────

@router.get("/start-call")
async def start_call(deal_id: str = Query(default="")):
    """Called when sales rep clicks "Start Call in Scriptius" on the CRM card.

    Fetches deal+contact data, stores it for prefill, and redirects to Scriptius.
    """
    if deal_id:
        data = await fetch_deal_with_contact(deal_id)
        if data:
            set_prefill(data)
            logger.info(f"Start call prefill for deal {deal_id}: {[k for k in data if not k.startswith('_')]}")

    # Return a simple HTML page that confirms and links to Scriptius
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Scriptius</title>
<style>
  body {{ font-family: -apple-system, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background: #f5f8fa; }}
  .card {{ text-align: center; padding: 32px; background: #fff; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
  h2 {{ color: #33475b; margin-bottom: 8px; }}
  p {{ color: #7c98b6; margin-bottom: 20px; }}
  .check {{ font-size: 48px; margin-bottom: 16px; }}
  a {{ display: inline-block; padding: 12px 32px; background: #ff7a59; color: #fff; text-decoration: none; border-radius: 6px; font-weight: 600; }}
  a:hover {{ background: #ff5c35; }}
</style></head>
<body>
  <div class="card">
    <div class="check">&#10003;</div>
    <h2>Client data loaded</h2>
    <p>Click "Start Call" in Scriptius to begin.</p>
    <a href="{SCRIPTIUS_URL}" target="_top">Open Scriptius</a>
  </div>
</body>
</html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html)


@router.get("/fetch-deal")
async def fetch_deal_json(deal_id: str = Query(default="")):
    """Fetch deal+contact data and return as JSON (used by frontend)."""
    if not deal_id:
        return {"status": "error", "message": "no deal_id"}
    data = await fetch_deal_with_contact(deal_id)
    if not data:
        return {"status": "error", "message": "could not fetch deal"}
    # Return only client card fields
    CARD_FIELDS = {"name", "role", "company", "industry", "experience"}
    client_fields = {k: v for k, v in data.items() if k in CARD_FIELDS and v}
    set_prefill(data)
    logger.info(f"Fetch deal {deal_id}: {client_fields}")
    return {"status": "ok", "clientProfile": client_fields}


# ── Manual prefill (for testing or non-HubSpot CRMs) ───────────────────────

@router.post("/prefill")
async def manual_prefill(request: Request):
    body = await request.json()
    logger.info(f"Manual prefill: {str(body)[:300]}")

    result = {}
    for key, value in body.items():
        if value and isinstance(value, str):
            result[key] = value.strip()

    if result:
        set_prefill(result)
    return {"status": "ok", "prefill": result}
