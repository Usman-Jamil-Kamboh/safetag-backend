// lib/screens/history_screen.dart
// ─────────────────────────────────────────────────────────────
// Screen 2 — Call History (Home Screen after login)
// Shows list of incoming calls + owner info at top
// ─────────────────────────────────────────────────────────────

import 'dart:convert';
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:web_socket_channel/web_socket_channel.dart';
import '../constants.dart';
import '../services/auth_service.dart';
import 'login_screen.dart';
import 'call_screen.dart';

class HistoryScreen extends StatefulWidget {
  const HistoryScreen({super.key});

  @override
  State<HistoryScreen> createState() => _HistoryScreenState();
}

class _HistoryScreenState extends State<HistoryScreen> {
  // Owner info
  String _ownerName  = '';
  String _stickerId  = '';
  String _phone      = '';

  // Call history
  List<Map<String, dynamic>> _calls = [];
  bool _loadingHistory = true;
  String? _historyError;

  // WebSocket for incoming calls
  WebSocketChannel? _wsChannel;
  Timer? _pingTimer;
  bool _wsConnected = false;

  @override
  void initState() {
    super.initState();
    _loadSession();
  }

  @override
  void dispose() {
    _pingTimer?.cancel();
    _wsChannel?.sink.close();
    super.dispose();
  }

  // ── Load saved session and then fetch data ──
  Future<void> _loadSession() async {
    final session = await AuthService.getSession();
    setState(() {
      _ownerName = session['ownerName'] ?? 'Owner';
      _stickerId = session['stickerId'] ?? '';
      _phone     = session['phone'] ?? '';
    });
    await _fetchHistory();
    _connectWebSocket();
  }

  // ── Fetch call history from backend ──
  Future<void> _fetchHistory() async {
    setState(() { _loadingHistory = true; _historyError = null; });
    try {
      final headers = await AuthService.authHeaders();
      final resp = await http.get(
        Uri.parse(EP_CALL_HISTORY),
        headers: headers,
      ).timeout(const Duration(seconds: 15));

      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body);
        setState(() {
          _calls = List<Map<String, dynamic>>.from(data['calls'] ?? []);
          _loadingHistory = false;
        });
      } else if (resp.statusCode == 401) {
        // Token expired — logout
        await AuthService.logout();
        if (!mounted) return;
        Navigator.pushReplacement(context,
          MaterialPageRoute(builder: (_) => const LoginScreen()));
      } else {
        setState(() {
          _historyError = 'Could not load call history.';
          _loadingHistory = false;
        });
      }
    } catch (e) {
      setState(() {
        _historyError = 'No internet connection.';
        _loadingHistory = false;
      });
    }
  }

  // ── Connect WebSocket to listen for incoming calls ──
  void _connectWebSocket() async {
    if (_stickerId.isEmpty) return;

    try {
      final url = wsOwnerUrl(_stickerId);
      _wsChannel = WebSocketChannel.connect(Uri.parse(url));
      setState(() => _wsConnected = true);

      // Listen for messages from signalling server
      _wsChannel!.stream.listen(
        (raw) {
          final msg = jsonDecode(raw as String);
          _handleSignalMessage(msg);
        },
        onDone: () {
          setState(() => _wsConnected = false);
          // Reconnect after 5 seconds
          Future.delayed(const Duration(seconds: 5), _connectWebSocket);
        },
        onError: (_) {
          setState(() => _wsConnected = false);
          Future.delayed(const Duration(seconds: 5), _connectWebSocket);
        },
      );

      // Send ping every 25 seconds to keep connection alive
      _pingTimer?.cancel();
      _pingTimer = Timer.periodic(const Duration(seconds: 25), (_) {
        try {
          _wsChannel?.sink.add(jsonEncode({'type': 'ping'}));
        } catch (_) {}
      });

    } catch (e) {
      setState(() => _wsConnected = false);
      Future.delayed(const Duration(seconds: 5), _connectWebSocket);
    }
  }

  // ── Handle incoming signal messages ──
  void _handleSignalMessage(Map<String, dynamic> msg) {
    final type = msg['type'];

    if (type == 'incoming_call') {
      // Show incoming call screen
      if (!mounted) return;
      Navigator.push(
        context,
        MaterialPageRoute(
          builder: (_) => CallScreen(
            stickerId: _stickerId,
            wsChannel: _wsChannel!,
            isIncoming: true,
          ),
        ),
      ).then((_) => _fetchHistory()); // refresh history after call ends
    }
  }

  // ── Logout ──
  Future<void> _logout() async {
    await AuthService.logout();
    if (!mounted) return;
    Navigator.pushReplacement(context,
      MaterialPageRoute(builder: (_) => const LoginScreen()));
  }

  // ── Format duration in seconds to mm:ss ──
  String _formatDuration(int? seconds) {
    if (seconds == null || seconds == 0) return '0:00';
    final m = seconds ~/ 60;
    final s = seconds % 60;
    return '$m:${s.toString().padLeft(2, '0')}';
  }

  // ── Format timestamp ──
  String _formatTime(String? iso) {
    if (iso == null) return '';
    try {
      final dt = DateTime.parse(iso).toLocal();
      final now = DateTime.now();
      if (dt.day == now.day && dt.month == now.month && dt.year == now.year) {
        return 'Today ${dt.hour.toString().padLeft(2,'0')}:${dt.minute.toString().padLeft(2,'0')}';
      }
      return '${dt.day}/${dt.month}/${dt.year} ${dt.hour.toString().padLeft(2,'0')}:${dt.minute.toString().padLeft(2,'0')}';
    } catch (_) {
      return iso;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0D0F14),
      appBar: AppBar(
        backgroundColor: const Color(0xFF13161F),
        elevation: 0,
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(_ownerName, style: const TextStyle(
              color: Colors.white, fontSize: 16, fontWeight: FontWeight.w700,
            )),
            Text(_stickerId, style: const TextStyle(
              color: Color(0xFF3B82F6), fontSize: 12,
            )),
          ],
        ),
        actions: [
          // Online indicator
          Padding(
            padding: const EdgeInsets.only(right: 8),
            child: Center(
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
                decoration: BoxDecoration(
                  color: _wsConnected
                      ? const Color(0xFF14532D).withOpacity(0.4)
                      : const Color(0xFF1F2937),
                  borderRadius: BorderRadius.circular(20),
                  border: Border.all(
                    color: _wsConnected
                        ? const Color(0xFF22C55E).withOpacity(0.4)
                        : const Color(0xFF374151),
                  ),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Container(
                      width: 7, height: 7,
                      decoration: BoxDecoration(
                        color: _wsConnected
                            ? const Color(0xFF22C55E)
                            : const Color(0xFF6B7280),
                        shape: BoxShape.circle,
                      ),
                    ),
                    const SizedBox(width: 6),
                    Text(
                      _wsConnected ? 'Online' : 'Offline',
                      style: TextStyle(
                        color: _wsConnected
                            ? const Color(0xFF22C55E)
                            : const Color(0xFF6B7280),
                        fontSize: 11,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
          // Logout
          IconButton(
            icon: const Icon(Icons.logout, color: Color(0xFF6B7280), size: 20),
            onPressed: _logout,
          ),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(1),
          child: Container(height: 1, color: const Color(0xFF1E2230)),
        ),
      ),
      body: RefreshIndicator(
        onRefresh: _fetchHistory,
        color: const Color(0xFF3B82F6),
        backgroundColor: const Color(0xFF1E2230),
        child: _buildBody(),
      ),
    );
  }

  Widget _buildBody() {
    if (_loadingHistory) {
      return const Center(
        child: CircularProgressIndicator(color: Color(0xFF3B82F6)),
      );
    }

    if (_historyError != null) {
      return Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(Icons.wifi_off_outlined, color: Color(0xFF6B7280), size: 48),
            const SizedBox(height: 16),
            Text(_historyError!, style: const TextStyle(color: Color(0xFF6B7280))),
            const SizedBox(height: 16),
            TextButton(
              onPressed: _fetchHistory,
              child: const Text('Retry', style: TextStyle(color: Color(0xFF3B82F6))),
            ),
          ],
        ),
      );
    }

    if (_calls.isEmpty) {
      return ListView(
        children: const [
          SizedBox(height: 120),
          Center(
            child: Column(
              children: [
                Icon(Icons.phone_missed_outlined, color: Color(0xFF374151), size: 64),
                SizedBox(height: 16),
                Text('No calls yet', style: TextStyle(
                  color: Color(0xFF6B7280), fontSize: 18, fontWeight: FontWeight.w600,
                )),
                SizedBox(height: 8),
                Text('Incoming calls will appear here', style: TextStyle(
                  color: Color(0xFF374151), fontSize: 14,
                )),
              ],
            ),
          ),
        ],
      );
    }

    return ListView.builder(
      padding: const EdgeInsets.all(16),
      itemCount: _calls.length,
      itemBuilder: (context, i) {
        final call = _calls[i];
        final duration = call['duration_seconds'] as int? ?? 0;
        final status   = call['status'] as String? ?? 'completed';
        final isMissed = status == 'missed' || duration == 0;

        return Container(
          margin: const EdgeInsets.only(bottom: 10),
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(
            color: const Color(0xFF181C27),
            border: Border.all(color: const Color(0xFF232840)),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Row(
            children: [
              // Icon
              Container(
                width: 42, height: 42,
                decoration: BoxDecoration(
                  color: isMissed
                      ? const Color(0xFF7F1D1D).withOpacity(0.3)
                      : const Color(0xFF14532D).withOpacity(0.3),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Icon(
                  isMissed ? Icons.phone_missed : Icons.phone_in_talk,
                  color: isMissed
                      ? const Color(0xFFEF4444)
                      : const Color(0xFF22C55E),
                  size: 20,
                ),
              ),
              const SizedBox(width: 14),
              // Details
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      call['caller_label'] as String? ?? 'Scanner',
                      style: const TextStyle(
                        color: Colors.white, fontSize: 15, fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 3),
                    Text(
                      _formatTime(call['started_at'] as String?),
                      style: const TextStyle(color: Color(0xFF6B7280), fontSize: 12),
                    ),
                  ],
                ),
              ),
              // Duration
              Text(
                isMissed ? 'Missed' : _formatDuration(duration),
                style: TextStyle(
                  color: isMissed
                      ? const Color(0xFFEF4444)
                      : const Color(0xFF9CA3AF),
                  fontSize: 13,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}