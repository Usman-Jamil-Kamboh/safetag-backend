// lib/screens/call_screen.dart
// ─────────────────────────────────────────────────────────────
// Screen 3 — Active / Incoming Call
// Updated to use OwnerWsService singleton instead of raw WS
// ─────────────────────────────────────────────────────────────

import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import '../screens/history_screen.dart';

class CallScreen extends StatefulWidget {
  final String stickerId;
  final OwnerWsService wsService;
  final bool isIncoming;

  const CallScreen({
    super.key,
    required this.stickerId,
    required this.wsService,
    required this.isIncoming,
  });

  @override
  State<CallScreen> createState() => _CallScreenState();
}

class _CallScreenState extends State<CallScreen> {
  bool _callAccepted = false;
  bool _callEnded    = false;
  bool _micMuted     = false;

  RTCPeerConnection? _peerConnection;
  MediaStream? _localStream;

  int _seconds = 0;
  Timer? _callTimer;

  final List<RTCIceCandidate> _pendingCandidates = [];
  bool _remoteDescSet = false;

  @override
  void initState() {
    super.initState();
    // Wire signal handler to this screen
    widget.wsService.onMessage = _handleSignal;
  }

  @override
  void dispose() {
    _callTimer?.cancel();
    _localStream?.dispose();
    _peerConnection?.close();
    // Restore signal handler to history screen callbacks
    // (HistoryScreen will re-register when it comes back)
    super.dispose();
  }

  void _handleSignal(Map<String, dynamic> msg) async {
    final type = msg['type'];
    if (type == 'offer')      await _handleOffer(msg['sdp']);
    else if (type == 'ice')   await _handleIce(msg['candidate']);
    else if (type == 'end')   _onCallEnded();
  }

  bool _settingUp = false;

  Future<void> _acceptCall() async {
    if (_settingUp || _callAccepted) return; // ignore double-tap on Accept
    _settingUp = true;
    setState(() => _callAccepted = true);
    await _setupWebRTC();
    _settingUp = false;
  }

  void _rejectCall() {
    widget.wsService.sendMessage({'type': 'reject'});
    Navigator.pop(context);
  }

  Future<void> _setupWebRTC() async {
    final config = {
      'iceServers': [
        {'urls': 'stun:stun.l.google.com:19302'},
        {'urls': 'stun:stun1.l.google.com:19302'},
      ],
      'sdpSemantics': 'unified-plan',
    };

    _peerConnection = await createPeerConnection(config);

    _localStream = await navigator.mediaDevices.getUserMedia({
      'audio': true, 'video': false,
    });

    for (final track in _localStream!.getAudioTracks()) {
      await _peerConnection!.addTrack(track, _localStream!);
    }

    _peerConnection!.onIceCandidate = (candidate) {
      widget.wsService.sendMessage({'type': 'ice', 'candidate': candidate.toMap()});
    };

    _peerConnection!.onConnectionState = (state) {
      if (state == RTCPeerConnectionState.RTCPeerConnectionStateConnected) {
        _startTimer();
      } else if (
        state == RTCPeerConnectionState.RTCPeerConnectionStateFailed ||
        state == RTCPeerConnectionState.RTCPeerConnectionStateDisconnected
      ) {
        _onCallEnded();
      }
    };
  }

  Future<void> _handleOffer(String sdp) async {
    if (_peerConnection == null) return;
    await _peerConnection!.setRemoteDescription(RTCSessionDescription(sdp, 'offer'));
    _remoteDescSet = true;
    for (final c in _pendingCandidates) {
      await _peerConnection!.addCandidate(c);
    }
    _pendingCandidates.clear();
    final answer = await _peerConnection!.createAnswer();
    await _peerConnection!.setLocalDescription(answer);
    widget.wsService.sendMessage({'type': 'answer', 'sdp': answer.sdp});
  }

  Future<void> _handleIce(Map<String, dynamic> candidateMap) async {
    final candidate = RTCIceCandidate(
      candidateMap['candidate'],
      candidateMap['sdpMid'],
      candidateMap['sdpMLineIndex'],
    );
    if (_remoteDescSet && _peerConnection != null) {
      await _peerConnection!.addCandidate(candidate);
    } else {
      _pendingCandidates.add(candidate);
    }
  }

  void _startTimer() {
    _callTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() => _seconds++);
    });
  }

  void _endCall() {
    widget.wsService.sendMessage({'type': 'end'});
    _onCallEnded();
  }

  void _onCallEnded() {
    if (_callEnded) return;
    setState(() => _callEnded = true);
    _callTimer?.cancel();
    _localStream?.dispose();
    _peerConnection?.close();
    if (mounted) Navigator.pop(context);
  }

  void _toggleMic() {
    if (_localStream == null) return;
    final enabled = !_micMuted;
    for (final track in _localStream!.getAudioTracks()) {
      track.enabled = enabled;
    }
    setState(() => _micMuted = !_micMuted);
  }

  String get _timerString {
    final m = _seconds ~/ 60;
    final s = _seconds % 60;
    return '$m:${s.toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0D0F14),
      body: SafeArea(
        child: _callAccepted ? _buildActiveCall() : _buildIncomingCall(),
      ),
    );
  }

  Widget _buildIncomingCall() {
    return Padding(
      padding: const EdgeInsets.all(32),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Container(
            width: 100, height: 100,
            decoration: BoxDecoration(
              color: const Color(0xFF1E3A5F),
              shape: BoxShape.circle,
              border: Border.all(color: const Color(0xFF3B82F6), width: 2),
            ),
            child: const Icon(Icons.person_outline, color: Color(0xFF3B82F6), size: 50),
          ),
          const SizedBox(height: 24),
          const Text('Incoming Call', style: TextStyle(color: Color(0xFF9CA3AF), fontSize: 14, letterSpacing: 1)),
          const SizedBox(height: 8),
          const Text('Scanner', style: TextStyle(color: Colors.white, fontSize: 28, fontWeight: FontWeight.w800)),
          const SizedBox(height: 6),
          Text('from sticker ${widget.stickerId}', style: const TextStyle(color: Color(0xFF6B7280), fontSize: 14)),
          const SizedBox(height: 60),
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              GestureDetector(
                onTap: _rejectCall,
                child: Container(
                  width: 70, height: 70,
                  decoration: const BoxDecoration(color: Color(0xFFEF4444), shape: BoxShape.circle),
                  child: const Icon(Icons.call_end, color: Colors.white, size: 30),
                ),
              ),
              const SizedBox(width: 48),
              GestureDetector(
                onTap: _acceptCall,
                child: Container(
                  width: 70, height: 70,
                  decoration: const BoxDecoration(color: Color(0xFF22C55E), shape: BoxShape.circle),
                  child: const Icon(Icons.call, color: Colors.white, size: 30),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildActiveCall() {
    return Padding(
      padding: const EdgeInsets.all(32),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Container(
            width: 90, height: 90,
            decoration: BoxDecoration(
              color: const Color(0xFF14532D).withOpacity(0.3),
              shape: BoxShape.circle,
              border: Border.all(color: const Color(0xFF22C55E), width: 2),
            ),
            child: const Icon(Icons.person_outline, color: Color(0xFF22C55E), size: 44),
          ),
          const SizedBox(height: 20),
          const Text('Scanner', style: TextStyle(color: Colors.white, fontSize: 26, fontWeight: FontWeight.w700)),
          const SizedBox(height: 8),
          Text(_timerString, style: const TextStyle(
            color: Color(0xFF22C55E), fontSize: 20, fontWeight: FontWeight.w600,
          )),
          const SizedBox(height: 8),
          const Text('Call in progress', style: TextStyle(color: Color(0xFF6B7280), fontSize: 13)),
          const SizedBox(height: 64),
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              GestureDetector(
                onTap: _toggleMic,
                child: Container(
                  width: 60, height: 60,
                  decoration: BoxDecoration(
                    color: _micMuted ? const Color(0xFFEF4444) : const Color(0xFF1E2230),
                    shape: BoxShape.circle,
                    border: Border.all(
                      color: _micMuted ? const Color(0xFFEF4444) : const Color(0xFF232840),
                    ),
                  ),
                  child: Icon(_micMuted ? Icons.mic_off : Icons.mic, color: Colors.white, size: 24),
                ),
              ),
              const SizedBox(width: 40),
              GestureDetector(
                onTap: _endCall,
                child: Container(
                  width: 72, height: 72,
                  decoration: const BoxDecoration(color: Color(0xFFEF4444), shape: BoxShape.circle),
                  child: const Icon(Icons.call_end, color: Colors.white, size: 32),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}