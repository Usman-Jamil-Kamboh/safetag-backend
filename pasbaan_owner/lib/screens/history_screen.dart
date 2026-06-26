// lib/screens/history_screen.dart

import 'dart:convert';
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:web_socket_channel/io.dart';
import '../constants.dart';
import '../services/auth_service.dart';
import 'login_screen.dart';
import 'call_screen.dart';

// ── Global WebSocket Service Singleton ──
class OwnerWsService {
  static final OwnerWsService _i = OwnerWsService._();
  static OwnerWsService get instance => _i;
  OwnerWsService._();

  IOWebSocketChannel? _channel;
  StreamSubscription? _channelSub;     // track the active listener explicitly
  Timer? _pingTimer;
  Timer? _reconnectTimer;
  String? _qrId;
  bool _intentionalClose = false;
  bool _isConnecting = false;          // guards against overlapping connect attempts

  Function(Map<String, dynamic>)? onMessage;
  Function(bool)? onConnectionChange;

  bool get isConnected => _channel != null;

  void connect(String qrId) {
    _qrId = qrId;
    _intentionalClose = false;
    _doConnect();
  }

  void _doConnect() {
    if (_qrId == null || _intentionalClose) return;
    // Guard: never start a new connect while one is already in
    // progress or already connected — this is what was causing
    // overlapping listeners ("Stream has already been listened to").
    if (_isConnecting || _channel != null) return;

    _isConnecting = true;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;

    // Always tear down any stale listener/channel before creating a new one.
    _cleanupChannel();

    try {
      final newChannel = IOWebSocketChannel.connect(Uri.parse(wsOwnerUrl(_qrId!)));
      _channel = newChannel;
      _isConnecting = false;
      onConnectionChange?.call(true);

      _channelSub = newChannel.stream.listen(
        (raw) {
          try { onMessage?.call(jsonDecode(raw as String)); } catch (_) {}
        },
        onDone: () => _onDisconnect(newChannel),
        onError: (_) => _onDisconnect(newChannel),
        cancelOnError: false,
      );

      _pingTimer?.cancel();
      _pingTimer = Timer.periodic(const Duration(seconds: 25), (_) {
        _send({'type': 'ping'});
      });

    } catch (e) {
      _isConnecting = false;
      _onDisconnect(_channel);
    }
  }

  // Cancels the current subscription and closes the current channel
  // (if any) WITHOUT touching _qrId/_intentionalClose, so it's safe to
  // call before every reconnect attempt.
  void _cleanupChannel() {
    _channelSub?.cancel();
    _channelSub = null;
    try { _channel?.sink.close(); } catch (_) {}
    _channel = null;
  }

  // [staleChannel] makes sure a late onDone/onError from an OLD channel
  // (that has already been replaced) can't stomp on a newer connection.
  void _onDisconnect([IOWebSocketChannel? staleChannel]) {
    if (staleChannel != null && staleChannel != _channel) return;
    _isConnecting = false;
    _cleanupChannel();
    _pingTimer?.cancel();
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
    _isConnecting = false;
    _pingTimer?.cancel();
    _reconnectTimer?.cancel();
    _cleanupChannel();
    onConnectionChange?.call(false);
  }
}

// ── History Screen ──
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
        // Re-register callbacks after returning from call screen
        OwnerWsService.instance.onMessage = _handleSignal;
        OwnerWsService.instance.onConnectionChange = (c) {
          if (mounted) setState(() => _wsConnected = c);
        };
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