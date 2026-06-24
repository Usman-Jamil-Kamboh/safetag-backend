// lib/constants.dart
// ─────────────────────────────────────────────────────────────
// Central config for the Pasbaan Owner App.
// Change BASE_URL here if your backend URL ever changes.
// ─────────────────────────────────────────────────────────────

const String BASE_URL = "https://api.pasbaan.com";

// HTTP endpoints
const String EP_REQUEST_OTP   = "$BASE_URL/app/request-otp";
const String EP_VERIFY_OTP    = "$BASE_URL/app/verify-otp";
const String EP_ME            = "$BASE_URL/app/me";
const String EP_CALL_HISTORY  = "$BASE_URL/app/call-history";
const String EP_TURN_CREDS    = "$BASE_URL/ws/turn-credentials";

// WebSocket endpoints (wss = secure WebSocket, same as https but for WS)
const String WS_BASE          = "wss://api.pasbaan.com";

String wsOwnerUrl(String qrId) => "$WS_BASE/ws/call/$qrId/owner";