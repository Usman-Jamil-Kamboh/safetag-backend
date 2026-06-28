"""
Pasbaan Pakistan — WebRTC Signalling Server
============================================
Phase 2 of the Owner App roadmap.

HOW TO USE:
  1. Place this file alongside main.py and app_routes.py
  2. Add these two lines to main.py right after the app_routes import block:
        from signalling import signalling_router
        app.include_router(signalling_router)
  3. No new packages needed — FastAPI WebSocket is already built in.
  4. No new DB tables needed.
  5. No new environment variables needed.

WHAT THIS FILE DOES:
  - Keeps a live registry of which owners are online (in memory)
  - Browser (scanner) connects to WS /ws/call/{qr_id}/scanner
  - Owner app connects to    WS /ws/call/{qr_id}/owner
  - Messages flow between them to set up a WebRTC call:
      browser  → sends  "offer"       → owner app
      owner    → sends  "answer"      → browser
      both     → send   "ice"         → each other
      owner    → sends  "reject"      → browser  (if they decline)
      either   → sends  "end"         → other    (hang up)

HOW WebRTC WORKS (simple explanation):
  WebRTC is the actual voice call — it goes browser ↔ owner DIRECTLY
  (peer to peer, no server in the middle for audio).
  But before two devices can talk directly, they need to exchange
  some setup information (called offer/answer/ICE candidates).
  That exchange happens through THIS signalling server via WebSocket.
  Once the exchange is done, audio flows directly between devices.
  Think of it like giving two people each other's address so they
  can meet — the server helps them find each other, then steps back.

TURN SERVER:
  For calls over mobile data (not WiFi), you need a TURN server.
  Sign up free at https://www.metered.ca/tools/openrelay/
  They give you free TURN credentials. Add to Render env vars:
    TURN_URL       → turns:openrelay.metered.ca:443
    TURN_USERNAME  → (from metered.ca dashboard)
    TURN_PASSWORD  → (from metered.ca dashboard)
  Without this, calls will work on WiFi but may fail on mobile data.
"""

import asyncio
import json
import os
from typing import Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fcm_push import send_incoming_call_push
from app_routes import get_fcm_token_for_qr

# ─────────────────────────────────────────────────────────────────────────────
# TURN SERVER CONFIG  (set these in Render env vars)
# ─────────────────────────────────────────────────────────────────────────────

TURN_URL      = os.getenv("TURN_URL",      "")
TURN_USERNAME = os.getenv("TURN_USERNAME", "")
TURN_PASSWORD = os.getenv("TURN_PASSWORD", "")

# ─────────────────────────────────────────────────────────────────────────────
# ROUTER
# ─────────────────────────────────────────────────────────────────────────────

signalling_router = APIRouter(tags=["WebRTC Signalling"])

# ─────────────────────────────────────────────────────────────────────────────
# IN-MEMORY ROOM REGISTRY
# Stores active WebSocket connections per sticker room.
# Format: { "ST-000001": { "owner": <WebSocket>, "scanner": <WebSocket> } }
# This resets if the server restarts — that is fine for calls.
# ─────────────────────────────────────────────────────────────────────────────

rooms: dict[str, dict[str, Optional[WebSocket]]] = {}


def _get_room(qr_id: str) -> dict:
    """Get or create a room for this sticker."""
    if qr_id not in rooms:
        rooms[qr_id] = {"owner": None, "scanner": None}
    return rooms[qr_id]


def _cleanup_room(qr_id: str):
    """Remove room from memory if both sides are gone."""
    room = rooms.get(qr_id)
    if room and room["owner"] is None and room["scanner"] is None:
        rooms.pop(qr_id, None)


async def _send(ws: Optional[WebSocket], message: dict):
    """Safely send a JSON message to a WebSocket. Ignores if connection is gone."""
    if ws is None:
        return
    try:
        await ws.send_text(json.dumps(message))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 1 — GET /ws/status/{qr_id}
# Simple HTTP endpoint the Flutter app polls to know if it should
# show "Online" or "Offline" indicator. Not WebSocket — just JSON.
# ─────────────────────────────────────────────────────────────────────────────

from fastapi.responses import JSONResponse

@signalling_router.get("/ws/status/{qr_id}")
async def owner_status(qr_id: str):
    """
    Returns whether the owner's app is currently connected for this sticker.
    Browser contact page can call this to show "Owner is online" before calling.

    Returns: { "online": true/false, "qr_id": "ST-000001" }
    """
    qr_id = qr_id.upper()
    room  = rooms.get(qr_id)
    online = room is not None and room.get("owner") is not None
    return JSONResponse({"online": online, "qr_id": qr_id})


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 2 — WS /ws/call/{qr_id}/owner
# The Flutter owner app connects here when it opens.
# Stays connected as long as app is open (keep-alive ping every 25s from app).
# ─────────────────────────────────────────────────────────────────────────────

@signalling_router.websocket("/ws/call/{qr_id}/owner")
async def owner_ws(websocket: WebSocket, qr_id: str):
    """
    Owner app WebSocket connection.

    Messages the owner can RECEIVE (from scanner via server):
      { "type": "incoming_call", "from": "scanner" }
      { "type": "offer",         "sdp": "..." }
      { "type": "ice",           "candidate": {...} }
      { "type": "end" }

    Messages the owner can SEND (relayed to scanner):
      { "type": "answer",  "sdp": "..." }
      { "type": "ice",     "candidate": {...} }
      { "type": "reject" }
      { "type": "end" }
      { "type": "ping" }   ← keep-alive, server replies with pong
    """
    qr_id = qr_id.upper()
    await websocket.accept()

    room = _get_room(qr_id)

    # If another owner connection exists, close the old one
    if room["owner"] is not None:
        try:
            await room["owner"].close()
        except Exception:
            pass

    room["owner"] = websocket
    print(f"[Signalling] Owner connected: {qr_id}", flush=True)

    # Tell scanner (if already waiting) that owner is now online
    await _send(room["scanner"], {"type": "owner_online"})

    # If a scanner is already waiting (e.g. owner app was just woken up by
    # a push notification), let the owner know there's an incoming call too.
    if room["scanner"] is not None:
        await _send(websocket, {"type": "incoming_call", "from": "scanner"})

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            # Keep-alive ping from app
            if msg_type == "ping":
                await _send(websocket, {"type": "pong"})
                continue

            # Owner answered — relay SDP answer to scanner
            if msg_type == "answer":
                await _send(room["scanner"], {"type": "answer", "sdp": msg.get("sdp")})

            # ICE candidate from owner — relay to scanner
            elif msg_type == "ice":
                await _send(room["scanner"], {"type": "ice", "candidate": msg.get("candidate")})

            # Owner rejected the call
            elif msg_type == "reject":
                await _send(room["scanner"], {"type": "rejected"})

            # Owner ended the call
            elif msg_type == "end":
                await _send(room["scanner"], {"type": "end"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[Signalling] Owner WS error ({qr_id}): {e}", flush=True)
    finally:
        # CRITICAL: only clear the slot if THIS websocket is still the one
        # registered. If a newer owner connection has already replaced us
        # (e.g. this is a stale/old connection whose disconnect is only
        # being detected late), don't stomp on the live connection.
        if room.get("owner") is websocket:
            room["owner"] = None
            _cleanup_room(qr_id)
            # Tell scanner the owner went offline
            await _send(room.get("scanner") if rooms.get(qr_id) else None,
                        {"type": "owner_offline"})
            print(f"[Signalling] Owner disconnected: {qr_id}", flush=True)
        else:
            print(f"[Signalling] Stale owner connection closed (ignored): {qr_id}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 3 — WS /ws/call/{qr_id}/scanner
# The browser contact page connects here when scanner taps "Call Owner".
# Short-lived — only connected during an active call attempt.
# ─────────────────────────────────────────────────────────────────────────────

@signalling_router.websocket("/ws/call/{qr_id}/scanner")
async def scanner_ws(websocket: WebSocket, qr_id: str):
    """
    Browser (scanner) WebSocket connection.

    Messages the scanner can RECEIVE (from owner via server):
      { "type": "owner_online" }
      { "type": "owner_offline" }
      { "type": "answer",   "sdp": "..." }
      { "type": "ice",      "candidate": {...} }
      { "type": "rejected" }
      { "type": "end" }

    Messages the scanner can SEND (relayed to owner):
      { "type": "offer", "sdp": "..." }
      { "type": "ice",   "candidate": {...} }
      { "type": "end" }
    """
    qr_id = qr_id.upper()
    await websocket.accept()

    room = _get_room(qr_id)

    # If a stale scanner connection from a previous attempt is still
    # registered, close it — otherwise its late disconnect cleanup can
    # wipe out THIS new, active connection a few seconds into the call.
    if room["scanner"] is not None:
        try:
            await room["scanner"].close()
        except Exception:
            pass

    room["scanner"] = websocket
    print(f"[Signalling] Scanner connected: {qr_id}", flush=True)

    # Immediately tell scanner whether owner is online
    owner_online = room["owner"] is not None
    await _send(websocket, {
        "type":   "status",
        "online": owner_online
    })

    # If owner is online, notify them of incoming call
    if owner_online:
        await _send(room["owner"], {"type": "incoming_call", "from": "scanner"})
    else:
        # Owner's app isn't connected (closed/killed/no network) — try to
        # wake it up with a push notification. The scanner side will
        # automatically retry once it gets an "owner_online" message.
        fcm_token = get_fcm_token_for_qr(qr_id)
        if fcm_token:
            print(f"[Signalling] Owner offline, attempting FCM push for {qr_id}", flush=True)
            send_incoming_call_push(qr_id, fcm_token)
        else:
            print(f"[Signalling] Owner offline, but NO fcm_token stored for {qr_id} — cannot push.", flush=True)

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            # SDP Offer from browser — relay to owner app
            if msg_type == "offer":
                await _send(room["owner"], {"type": "offer", "sdp": msg.get("sdp")})

            # ICE candidate from browser — relay to owner app
            elif msg_type == "ice":
                await _send(room["owner"], {"type": "ice", "candidate": msg.get("candidate")})

            # Scanner ended the call
            elif msg_type == "end":
                await _send(room["owner"], {"type": "end"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[Signalling] Scanner WS error ({qr_id}): {e}", flush=True)
    finally:
        # CRITICAL: only clear/notify if THIS websocket is still the one
        # registered as the active scanner — prevents a late-detected
        # disconnect from a stale/old connection killing a live call.
        if room.get("scanner") is websocket:
            room["scanner"] = None
            _cleanup_room(qr_id)
            # Tell owner scanner disconnected (in case call was active)
            if rooms.get(qr_id):
                await _send(room.get("owner"), {"type": "end"})
            print(f"[Signalling] Scanner disconnected: {qr_id}", flush=True)
        else:
            print(f"[Signalling] Stale scanner connection closed (ignored): {qr_id}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# TURN SERVER CREDENTIALS ENDPOINT
# Flutter app and browser both call this to get TURN credentials
# before starting a WebRTC call. Keeps credentials out of the app code.
# ─────────────────────────────────────────────────────────────────────────────

@signalling_router.get("/ws/turn-credentials")
async def turn_credentials():
    """
    Returns TURN server credentials for WebRTC.
    Both the browser and Flutter app call this before making a call.

    If TURN env vars are not set, returns only Google's free STUN server
    (works on WiFi, may fail on mobile data).

    Returns:
    {
      "ice_servers": [
        { "urls": "stun:stun.l.google.com:19302" },
        { "urls": "turns:...", "username": "...", "credential": "..." }
      ]
    }
    """
    # Google's free STUN server — always include this
    ice_servers = [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
    ]

    # Add TURN server if configured
    if TURN_URL and TURN_USERNAME and TURN_PASSWORD:
        ice_servers.append({
            "urls":       TURN_URL,
            "username":   TURN_USERNAME,
            "credential": TURN_PASSWORD,
        })

    return JSONResponse({"ice_servers": ice_servers})