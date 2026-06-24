// lib/screens/login_screen.dart
// ─────────────────────────────────────────────────────────────
// Screen 1 — Login
// Owner enters Sticker ID + Phone → gets OTP → enters OTP → logged in
// ─────────────────────────────────────────────────────────────

import 'package:flutter/material.dart';
import '../services/auth_service.dart';
import 'history_screen.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  // Controllers
  final _stickerCtrl = TextEditingController();
  final _phoneCtrl   = TextEditingController();
  final _otpCtrl     = TextEditingController();

  // State
  bool _otpSent   = false;   // false = show step1, true = show step2
  bool _loading   = false;
  String? _error;

  // ── Step 1: Request OTP ──
  Future<void> _requestOtp() async {
    final stickerId = _stickerCtrl.text.trim().toUpperCase();
    final phone     = _phoneCtrl.text.trim();

    if (stickerId.isEmpty || phone.isEmpty) {
      setState(() => _error = 'Please fill in both fields.');
      return;
    }

    setState(() { _loading = true; _error = null; });

    final err = await AuthService.requestOtp(
      stickerId: stickerId,
      phone:     phone,
    );

    setState(() { _loading = false; });

    if (err != null) {
      setState(() => _error = err);
    } else {
      setState(() { _otpSent = true; _error = null; });
    }
  }

  // ── Step 2: Verify OTP ──
  Future<void> _verifyOtp() async {
    final stickerId = _stickerCtrl.text.trim().toUpperCase();
    final phone     = _phoneCtrl.text.trim();
    final otp       = _otpCtrl.text.trim();

    if (otp.isEmpty) {
      setState(() => _error = 'Please enter the OTP.');
      return;
    }

    setState(() { _loading = true; _error = null; });

    final err = await AuthService.verifyOtp(
      stickerId: stickerId,
      phone:     phone,
      otp:       otp,
    );

    setState(() { _loading = false; });

    if (err != null) {
      setState(() => _error = err);
    } else {
      // Login successful — go to history screen
      if (!mounted) return;
      Navigator.pushReplacement(
        context,
        MaterialPageRoute(builder: (_) => const HistoryScreen()),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0D0F14),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const SizedBox(height: 40),

              // ── Logo ──
              Row(
                children: [
                  Container(
                    width: 48, height: 48,
                    decoration: BoxDecoration(
                      gradient: const LinearGradient(
                        colors: [Color(0xFF3B82F6), Color(0xFF6366F1)],
                        begin: Alignment.topLeft,
                        end: Alignment.bottomRight,
                      ),
                      borderRadius: BorderRadius.circular(14),
                    ),
                    child: const Icon(Icons.shield_outlined, color: Colors.white, size: 26),
                  ),
                  const SizedBox(width: 12),
                  const Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('Pasbaan', style: TextStyle(
                        color: Colors.white,
                        fontSize: 22,
                        fontWeight: FontWeight.w800,
                        letterSpacing: -0.5,
                      )),
                      Text('Owner Portal', style: TextStyle(
                        color: Color(0xFF6B7280),
                        fontSize: 13,
                      )),
                    ],
                  ),
                ],
              ),

              const SizedBox(height: 48),

              // ── Title ──
              Text(
                _otpSent ? 'Enter your OTP' : 'Owner Login',
                style: const TextStyle(
                  color: Colors.white,
                  fontSize: 28,
                  fontWeight: FontWeight.w800,
                  letterSpacing: -0.5,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                _otpSent
                    ? 'We sent a 6-digit code to ${_phoneCtrl.text}'
                    : 'Login with your sticker ID and registered phone number.',
                style: const TextStyle(color: Color(0xFF6B7280), fontSize: 14, height: 1.5),
              ),

              const SizedBox(height: 36),

              // ── Step 1 fields (hidden after OTP sent) ──
              if (!_otpSent) ...[
                _label('Sticker ID'),
                _input(
                  controller: _stickerCtrl,
                  hint: 'e.g. ST-000001',
                  icon: Icons.qr_code,
                  textCapitalization: TextCapitalization.characters,
                ),
                const SizedBox(height: 16),
                _label('Registered Phone Number'),
                _input(
                  controller: _phoneCtrl,
                  hint: '03001234567',
                  icon: Icons.phone_outlined,
                  keyboardType: TextInputType.phone,
                ),
                const SizedBox(height: 28),
                _primaryButton(
                  label: 'Send OTP',
                  onTap: _requestOtp,
                ),
              ],

              // ── Step 2 OTP field ──
              if (_otpSent) ...[
                _label('6-Digit OTP'),
                _input(
                  controller: _otpCtrl,
                  hint: '_ _ _ _ _ _',
                  icon: Icons.lock_outline,
                  keyboardType: TextInputType.number,
                  maxLength: 6,
                ),
                const SizedBox(height: 28),
                _primaryButton(
                  label: 'Verify & Login',
                  onTap: _verifyOtp,
                ),
                const SizedBox(height: 16),
                // Resend option
                Center(
                  child: TextButton(
                    onPressed: _loading ? null : () {
                      setState(() { _otpSent = false; _otpCtrl.clear(); _error = null; });
                    },
                    child: const Text(
                      'Change number or resend OTP',
                      style: TextStyle(color: Color(0xFF3B82F6), fontSize: 14),
                    ),
                  ),
                ),
              ],

              // ── Error message ──
              if (_error != null) ...[
                const SizedBox(height: 16),
                Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: const Color(0xFF7F1D1D).withOpacity(0.3),
                    border: Border.all(color: const Color(0xFFEF4444).withOpacity(0.4)),
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Row(
                    children: [
                      const Icon(Icons.error_outline, color: Color(0xFFEF4444), size: 18),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Text(_error!, style: const TextStyle(
                          color: Color(0xFFFCA5A5), fontSize: 13,
                        )),
                      ),
                    ],
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  // ── Reusable widgets ──

  Widget _label(String text) => Padding(
    padding: const EdgeInsets.only(bottom: 8),
    child: Text(text, style: const TextStyle(
      color: Color(0xFF9CA3AF), fontSize: 13, fontWeight: FontWeight.w500,
    )),
  );

  Widget _input({
    required TextEditingController controller,
    required String hint,
    required IconData icon,
    TextInputType keyboardType = TextInputType.text,
    TextCapitalization textCapitalization = TextCapitalization.none,
    int? maxLength,
  }) =>
    TextField(
      controller: controller,
      keyboardType: keyboardType,
      textCapitalization: textCapitalization,
      maxLength: maxLength,
      enabled: !_loading,
      style: const TextStyle(color: Colors.white, fontSize: 16),
      decoration: InputDecoration(
        hintText: hint,
        hintStyle: const TextStyle(color: Color(0xFF4B5563)),
        prefixIcon: Icon(icon, color: const Color(0xFF6B7280), size: 20),
        counterText: '',
        filled: true,
        fillColor: const Color(0xFF161920),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: Color(0xFF232840)),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: Color(0xFF232840)),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: Color(0xFF3B82F6), width: 1.5),
        ),
      ),
    );

  Widget _primaryButton({required String label, required VoidCallback onTap}) =>
    SizedBox(
      width: double.infinity,
      height: 52,
      child: ElevatedButton(
        onPressed: _loading ? null : onTap,
        style: ElevatedButton.styleFrom(
          backgroundColor: const Color(0xFF3B82F6),
          disabledBackgroundColor: const Color(0xFF1E3A5F),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          elevation: 0,
        ),
        child: _loading
            ? const SizedBox(
                width: 22, height: 22,
                child: CircularProgressIndicator(
                  color: Colors.white, strokeWidth: 2.5,
                ),
              )
            : Text(label, style: const TextStyle(
                color: Colors.white,
                fontSize: 16,
                fontWeight: FontWeight.w600,
              )),
      ),
    );
}