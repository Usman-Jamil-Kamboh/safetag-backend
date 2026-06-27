"""
Pasbaan Pakistan — FCM Push Notifications
==========================================
Wakes the owner's Flutter app (ring + vibrate) when a call comes in and
the app's WebSocket isn't currently connected (app closed/killed/no network).

HOW TO USE:
  1. Place this file alongside main.py, app_routes.py, signalling.py
  2. In Render dashboard, add an environment variable:
        FIREBASE_SERVICE_ACCOUNT_JSON  → paste the FULL content of the
        service account JSON file you downloaded from:
        Firebase Console → Project Settings → Service Accounts →
        "Generate new private key"
     (Paste the whole JSON as one line/value — Render handles multi-line
      values fine, just paste exactly what's in the file.)
  3. Install one new package:
        pip install firebase-admin
     Add it to requirements.txt too.
  4. In signalling.py, import and call send_incoming_call_push(qr_id)
     when the owner is offline (see the scanner_ws handler).

NOTE: This is DIFFERENT from google-services.json (that one goes in the
Flutter app, this one is for the SERVER to be allowed to send pushes).
Never commit the actual JSON file or put it directly in code — it's a
secret credential, keep it only in the Render env var.
"""

import os
import json
import firebase_admin
from firebase_admin import credentials, messaging

_firebase_app = None


def _get_firebase_app():
    """Lazily initialize the Firebase Admin app, once."""
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app

    raw_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if not raw_json:
        print("[FCM] WARNING: FIREBASE_SERVICE_ACCOUNT_JSON not set — push notifications disabled.", flush=True)
        return None

    try:
        service_account_info = json.loads(raw_json)
        cred = credentials.Certificate(service_account_info)
        _firebase_app = firebase_admin.initialize_app(cred)
        print("[FCM] Firebase Admin initialized successfully.", flush=True)
        return _firebase_app
    except Exception as e:
        print(f"[FCM] ERROR initializing Firebase Admin: {e}", flush=True)
        return None


def send_incoming_call_push(qr_id: str, fcm_token: str) -> bool:
    """
    Sends a high-priority push notification that rings + vibrates the
    owner's phone even if the app is fully closed, using a dedicated
    'incoming_calls' notification channel (created natively in the app).

    Returns True if the push was sent successfully, False otherwise.
    """
    app = _get_firebase_app()
    if app is None:
        return False
    if not fcm_token:
        return False

    try:
        message = messaging.Message(
            token=fcm_token,
            notification=messaging.Notification(
                title="Incoming Call",
                body="Someone is calling about your vehicle. Tap to answer.",
            ),
            data={
                "type":   "incoming_call",
                "qr_id":  qr_id,
            },
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="incoming_calls",
                    priority="max",
                    visibility="public",
                ),
            ),
        )
        messaging.send(message)
        print(f"[FCM] Push sent for {qr_id}", flush=True)
        return True
    except Exception as e:
        print(f"[FCM] ERROR sending push for {qr_id}: {e}", flush=True)
        return False