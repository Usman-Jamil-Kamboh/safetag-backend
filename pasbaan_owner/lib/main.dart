// lib/main.dart
// ─────────────────────────────────────────────────────────────
// Entry point for Pasbaan Owner App
// Checks if user is already logged in → routes to correct screen
// ─────────────────────────────────────────────────────────────

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'services/auth_service.dart';
import 'screens/login_screen.dart';
import 'screens/history_screen.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Lock to portrait mode
  await SystemChrome.setPreferredOrientations([
    DeviceOrientation.portraitUp,
  ]);

  // Check if user is already logged in
  final loggedIn = await AuthService.isLoggedIn();

  runApp(PasbaanOwnerApp(startLoggedIn: loggedIn));
}

class PasbaanOwnerApp extends StatelessWidget {
  final bool startLoggedIn;

  const PasbaanOwnerApp({super.key, required this.startLoggedIn});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Pasbaan Owner',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: const ColorScheme.dark(
          primary:   Color(0xFF3B82F6),
          secondary: Color(0xFF6366F1),
          surface:   Color(0xFF13161F),
        ),
        scaffoldBackgroundColor: const Color(0xFF0D0F14),
        fontFamily: 'SF Pro Display',
      ),
      // Go straight to history if already logged in, else login screen
      home: startLoggedIn ? const HistoryScreen() : const LoginScreen(),
    );
  }
}