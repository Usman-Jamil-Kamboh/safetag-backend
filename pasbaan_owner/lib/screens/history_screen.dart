cat > /mnt/user-data/outputs/history_screen.dart << 'DARTEOF'
// lib/screens/history_screen.dart

import 'dart:convert';
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:web_socket_channel/web_socket_channel.dart';
import '../constants.dart';
import '../services/auth_service.dart';
import 'login_screen.dart';
import 'call_screen.dart';

// ─────────────────────────────────────────────────────────────
// GLOBAL WebSocket SERVICE SINGLETON
// Creates a FRESH channel on every connect/reconnect
// to avoid "Stream has already been listened to" error.
// ─────────────────────────────────────────────────────────────
class OwnerWsService {
  static final OwnerWsService _instance = OwnerWsService._();
  static OwnerWsService get instance => _instance;
  OwnerWsService._();

  WebSocketChannel? _channel;
  StreamSubscription? _sub;
  Timer? _pingTimer;
  Timer? _reconnectTimer;
  String? _qrId;
  bool _intentionalClose = false;
  bool _connected = false;

  Function(Map<String, dynamic>)? onMessage;
  Function(bool)? onConnectionChange;

  bool get isConnected => _connected;

  void connect(String qrId) {
    _qrId = qrId;
    _intentionalClose = false;
    _doConnect();
  }

  void _doConnect() {
    if (_qrId == null || _intentionalClose) return;

    // Always cancel old subscription and close old channel first
    _sub?.cancel();
    _sub = null;
    try { _channel?.sink.close(); } catch (_) {}
    _channel = null;
    _pingTimer?.cancel();

    try {
      // Create a FRESH channel every time
      _channel = WebSocketChannel.connect(Uri.parse(wsOwnerUrl(_qrId!)));

      // Subscribe to the fresh channel's stream
      _sub = _channel!.stream.listen(
        (raw) {
          try { onMessage?.call(jsonDecode(raw as String)); } catch (_) {}
        },
        onDone: _onDisconnect,
        onError: (_) => _onDisconnect(),
        cancelOnError: false,
      );

      _connected = true;
      onConnectionChange?.call(true);

      // Ping every 25s to keep alive
      _pingTimer = Timer.periodic(const Duration(seconds: 25), (_) {
        _send({'type': 'ping'});
      });

    } catch (e) {
      _connected = false;
      _onDisconnect();
    }
  }

  void _onDisconnect() {
    _sub?.cancel();
    _sub = null;
    _pingTimer?.cancel();
    _connected = false;
    onConnectionChange?.call(false);

    if (!_intentionalClose) {
      _reconnectTimer?.cancel();
      _reconnectTimer = Timer(const Duration(seconds: 5), _doConnect);
    }
  }

  void _send(Map<String, dynamic> msg) {
    try { _channel?.sink.add(jsonEncode(msg)); } catch (_) {}
  }

  void sendMessage(Map<String, dynamic> msg) => _send(msg);

  void disconnect() {
    _intentionalClose = true;
    _pingTimer?.cancel();
    _reconnectTimer?.cancel();
    _sub?.cancel();
    _sub = null;
    try { _channel?.sink.close(); } catch (_) {}
    _channel = null;
    _connected = false;
    onConnectionChange?.call(false);
  }
}

// ─────────────────────────────────────────────────────────────
// HISTORY SCREEN
// ─────────────────────────────────────────────────────────────
class HistoryScreen extends StatefulWidget {
  const HistoryScreen({super.key});

  @override
  State<HistoryScreen> createState() => _HistoryScreenState();
}

class _HistoryScreenState extends State<HistoryScreen>
    with WidgetsBindingObserver {

  String _ownerName = '';
  String _stickerId = '';
  List<Map<String, dynamic>> _calls = [];
  bool _loadingHistory = true;
  String? _historyError;
  bool _wsConnected = false;
  bool _callScreenOpen = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _loadSession();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    OwnerWsService.instance.onMessage = null;
    OwnerWsService.instance.onConnectionChange = null;
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      if (!OwnerWsService.instance.isConnected && _stickerId.isNotEmpty) {
        OwnerWsService.instance.connect(_stickerId);
      }
      _fetchHistory();
    }
  }

  Future<void> _loadSession() async {
    final session = await AuthService.getSession();
    setState(() {
      _ownerName = session['ownerName'] ?? 'Owner';
      _stickerId = session['stickerId'] ?? '';
    });
    await _fetchHistory();
    _startWs();
  }

  void _startWs() {
    if (_stickerId.isEmpty) return;
    OwnerWsService.instance.onMessage = _handleSignal;
    OwnerWsService.instance.onConnectionChange = (c) {
      if (mounted) setState(() => _wsConnected = c);
    };
    OwnerWsService.instance.connect(_stickerId);
    setState(() => _wsConnected = OwnerWsService.instance.isConnected);
  }

  void _handleSignal(Map<String, dynamic> msg) {
    if (msg['type'] == 'incoming_call' && !_callScreenOpen) {
      _callScreenOpen = true;
      HapticFeedback.heavyImpact();
      if (!mounted) return;
      Navigator.push(
        context,
        MaterialPageRoute(
          builder: (_) => CallScreen(
            stickerId: _stickerId,
            wsService: OwnerWsService.instance,
            isIncoming: true,
          ),
        ),
      ).then((_) {
        _callScreenOpen = false;
        _fetchHistory();
      });
    }
  }

  Future<void> _fetchHistory() async {
    setState(() { _loadingHistory = true; _historyError = null; });
    try {
      final headers = await AuthService.authHeaders();
      final resp = await http.get(
        Uri.parse(EP_CALL_HISTORY), headers: headers,
      ).timeout(const Duration(seconds: 15));

      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body);
        setState(() {
          _calls = List<Map<String, dynamic>>.from(data['calls'] ?? []);
          _loadingHistory = false;
        });
      } else if (resp.statusCode == 401) {
        await AuthService.logout();
        if (!mounted) return;
        Navigator.pushReplacement(context,
          MaterialPageRoute(builder: (_) => const LoginScreen()));
      } else {
        setState(() { _historyError = 'Could not load call history.'; _loadingHistory = false; });
      }
    } catch (e) {
      setState(() { _historyError = 'No internet connection.'; _loadingHistory = false; });
    }
  }

  Future<void> _logout() async {
    OwnerWsService.instance.disconnect();
    await AuthService.logout();
    if (!mounted) return;
    Navigator.pushReplacement(context,
      MaterialPageRoute(builder: (_) => const LoginScreen()));
  }

  String _formatDuration(int? s) {
    if (s == null || s == 0) return '0:00';
    return '${s ~/ 60}:${(s % 60).toString().padLeft(2, '0')}';
  }

  String _formatTime(String? iso) {
    if (iso == null) return '';
    try {
      final dt = DateTime.parse(iso).toLocal();
      final now = DateTime.now();
      if (dt.day == now.day && dt.month == now.month && dt.year == now.year) {
        return 'Today ${dt.hour.toString().padLeft(2,'0')}:${dt.minute.toString().padLeft(2,'0')}';
      }
      return '${dt.day}/${dt.month}/${dt.year} ${dt.hour.toString().padLeft(2,'0')}:${dt.minute.toString().padLeft(2,'0')}';
    } catch (_) { return iso; }
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
            Text(_ownerName, style: const TextStyle(color: Colors.white, fontSize: 16, fontWeight: FontWeight.w700)),
            Text(_stickerId, style: const TextStyle(color: Color(0xFF3B82F6), fontSize: 12)),
          ],
        ),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 8),
            child: Center(
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
                decoration: BoxDecoration(
                  color: _wsConnected ? const Color(0xFF14532D).withOpacity(0.4) : const Color(0xFF1F2937),
                  borderRadius: BorderRadius.circular(20),
                  border: Border.all(
                    color: _wsConnected ? const Color(0xFF22C55E).withOpacity(0.4) : const Color(0xFF374151),
                  ),
                ),
                child: Row(mainAxisSize: MainAxisSize.min, children: [
                  Container(
                    width: 7, height: 7,
                    decoration: BoxDecoration(
                      color: _wsConnected ? const Color(0xFF22C55E) : const Color(0xFF6B7280),
                      shape: BoxShape.circle,
                    ),
                  ),
                  const SizedBox(width: 6),
                  Text(
                    _wsConnected ? 'Online' : 'Connecting...',
                    style: TextStyle(
                      color: _wsConnected ? const Color(0xFF22C55E) : const Color(0xFF6B7280),
                      fontSize: 11, fontWeight: FontWeight.w600,
                    ),
                  ),
                ]),
              ),
            ),
          ),
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
    if (_loadingHistory) return const Center(child: CircularProgressIndicator(color: Color(0xFF3B82F6)));
    if (_historyError != null) {
      return Center(child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
        const Icon(Icons.wifi_off_outlined, color: Color(0xFF6B7280), size: 48),
        const SizedBox(height: 16),
        Text(_historyError!, style: const TextStyle(color: Color(0xFF6B7280))),
        const SizedBox(height: 16),
        TextButton(onPressed: _fetchHistory, child: const Text('Retry', style: TextStyle(color: Color(0xFF3B82F6)))),
      ]));
    }
    if (_calls.isEmpty) {
      return ListView(children: const [
        SizedBox(height: 120),
        Center(child: Column(children: [
          Icon(Icons.phone_missed_outlined, color: Color(0xFF374151), size: 64),
          SizedBox(height: 16),
          Text('No calls yet', style: TextStyle(color: Color(0xFF6B7280), fontSize: 18, fontWeight: FontWeight.w600)),
          SizedBox(height: 8),
          Text('Incoming calls will appear here', style: TextStyle(color: Color(0xFF374151), fontSize: 14)),
        ])),
      ]);
    }
    return ListView.builder(
      padding: const EdgeInsets.all(16),
      itemCount: _calls.length,
      itemBuilder: (context, i) {
        final call = _calls[i];
        final duration = call['duration_seconds'] as int? ?? 0;
        final isMissed = (call['status'] as String? ?? '') == 'missed' || duration == 0;
        return Container(
          margin: const EdgeInsets.only(bottom: 10),
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(
            color: const Color(0xFF181C27),
            border: Border.all(color: const Color(0xFF232840)),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Row(children: [
            Container(
              width: 42, height: 42,
              decoration: BoxDecoration(
                color: isMissed ? const Color(0xFF7F1D1D).withOpacity(0.3) : const Color(0xFF14532D).withOpacity(0.3),
                borderRadius: BorderRadius.circular(10),
              ),
              child: Icon(isMissed ? Icons.phone_missed : Icons.phone_in_talk,
                color: isMissed ? const Color(0xFFEF4444) : const Color(0xFF22C55E), size: 20),
            ),
            const SizedBox(width: 14),
            Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(call['caller_label'] as String? ?? 'Scanner',
                style: const TextStyle(color: Colors.white, fontSize: 15, fontWeight: FontWeight.w600)),
              const SizedBox(height: 3),
              Text(_formatTime(call['started_at'] as String?),
                style: const TextStyle(color: Color(0xFF6B7280), fontSize: 12)),
            ])),
            Text(isMissed ? 'Missed' : _formatDuration(duration),
              style: TextStyle(
                color: isMissed ? const Color(0xFFEF4444) : const Color(0xFF9CA3AF),
                fontSize: 13, fontWeight: FontWeight.w500)),
          ]),
        );
      },
    );
  }
}
