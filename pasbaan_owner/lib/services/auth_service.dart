// lib/services/auth_service.dart
// ─────────────────────────────────────────────────────────────
// Handles login, OTP, and JWT token storage.
// ─────────────────────────────────────────────────────────────

import 'dart:convert';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;
import '../constants.dart';

class AuthService {
  static const _storage = FlutterSecureStorage();

  // ── Keys used in secure storage ──
  static const _keyToken      = 'jwt_token';
  static const _keyStickerId  = 'sticker_id';
  static const _keyOwnerName  = 'owner_name';
  static const _keyPhone      = 'phone';

  // ── Save token after login ──
  static Future<void> saveSession({
    required String token,
    required String stickerId,
    required String ownerName,
    required String phone,
  }) async {
    await _storage.write(key: _keyToken,     value: token);
    await _storage.write(key: _keyStickerId, value: stickerId);
    await _storage.write(key: _keyOwnerName, value: ownerName);
    await _storage.write(key: _keyPhone,     value: phone);
  }

  // ── Read saved session ──
  static Future<Map<String, String?>> getSession() async {
    return {
      'token':      await _storage.read(key: _keyToken),
      'stickerId':  await _storage.read(key: _keyStickerId),
      'ownerName':  await _storage.read(key: _keyOwnerName),
      'phone':      await _storage.read(key: _keyPhone),
    };
  }

  // ── Check if user is logged in ──
  static Future<bool> isLoggedIn() async {
    final token = await _storage.read(key: _keyToken);
    return token != null && token.isNotEmpty;
  }

  // ── Get token for API calls ──
  static Future<String?> getToken() async {
    return await _storage.read(key: _keyToken);
  }

  // ── Get sticker ID ──
  static Future<String?> getStickerId() async {
    return await _storage.read(key: _keyStickerId);
  }

  // ── Logout — clears everything ──
  static Future<void> logout() async {
    await _storage.deleteAll();
  }

  // ── Request OTP from backend ──
  // Returns null on success, error message string on failure
  static Future<String?> requestOtp({
    required String stickerId,
    required String phone,
  }) async {
    try {
      final resp = await http.post(
        Uri.parse(EP_REQUEST_OTP),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'sticker_id': stickerId, 'phone': phone}),
      ).timeout(const Duration(seconds: 15));

      final data = jsonDecode(resp.body);
      if (resp.statusCode == 200) return null; // success
      return data['detail'] ?? 'Something went wrong. Please try again.';
    } catch (e) {
      return 'Cannot reach server. Check your internet connection.';
    }
  }

  // ── Verify OTP and save session ──
  // Returns null on success, error message string on failure
  static Future<String?> verifyOtp({
    required String stickerId,
    required String phone,
    required String otp,
  }) async {
    try {
      final resp = await http.post(
        Uri.parse(EP_VERIFY_OTP),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'sticker_id': stickerId,
          'phone':      phone,
          'otp':        otp,
        }),
      ).timeout(const Duration(seconds: 15));

      final data = jsonDecode(resp.body);
      if (resp.statusCode == 200) {
        await saveSession(
          token:      data['token'],
          stickerId:  data['sticker_id'],
          ownerName:  data['owner_name'] ?? 'Owner',
          phone:      phone,
        );
        return null; // success
      }
      return data['detail'] ?? 'Incorrect OTP. Please try again.';
    } catch (e) {
      return 'Cannot reach server. Check your internet connection.';
    }
  }

  // ── Auth header for protected requests ──
  static Future<Map<String, String>> authHeaders() async {
    final token = await getToken();
    return {
      'Content-Type':  'application/json',
      'Authorization': 'Bearer $token',
    };
  }
}