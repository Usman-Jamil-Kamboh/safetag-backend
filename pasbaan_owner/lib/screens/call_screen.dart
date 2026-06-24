// lib/screens/call_screen.dart
// ─────────────────────────────────────────────────────────────
// Screen 3 — Active / Incoming Call
// Shows incoming call UI, accept/reject, active call with timer
// ─────────────────────────────────────────────────────────────

import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import '../constants.dart';

class CallScreen extends StatefulWidget {
  final String stickerId;
  final WebSocketChannel wsChannel;
  final bool isIncoming;

  const CallScreen({
    super.key,
    required this.stickerId,
    required this.wsChannel,
    required this.isIncoming,
  });

  @override
  State<CallScreen> createState() => _CallScreenState();
}

class _CallScreenState extends State<CallScreen> {
  // Call state
  bool _callAccepted = false;
  bool _callEnded    = false;
  bool _micMuted     = false;

  // WebRTC
  RTCPeerConnection? _peerConnection;
  MediaStream? _localStream;

  // Timer
  int _seconds = 0;
  Timer? _callTimer;

  // ICE candidates queued before remote description is set
  final List<RTCIceCandidate> _pendingCandidates = [];
  bool _remoteDescSet = false;

  @override
  void initState() {
    super.initState();
    // Listen for WebRTC signals
    widget.wsChannel.stream.listen(
      _handleSignal,
      onDone: _onCallEnded,
      onError: (_) => _onCallEnded(),
    );
  }

  @override
  void dispose() {
    _callTimer?.cancel();
    _localStream?.dispose();
    _peerConnection?.close();
    super.dispose();
  }

  // ── Handle incoming signals ──
  void _handleSignal(dynamic raw) async {
    final msg  = jsonDecode(raw as String);
    final type = msg['type'];

    if (type == 'offer') {
      await _handleOffer(msg['sdp']);
    } else if (type == 'ice') {
      await _handleIce(msg['candidate']);
    } else if (type == 'end') {
      _onCallEnded();
    }
  }

  // ── Accept call ──
  Future<void> _acceptCall() async {
    setState(() => _callAccepted = true);
    await _setupWebRTC();
  }

  // ── Reject call ──
  void _rejectCall() {
    widget.wsChannel.sink.add(jsonEncode({'type': 'reject'}));
    Navigator.pop(context);
  }

  // ── Set up WebRTC peer connection ──
  Future<void> _setupWebRTC() async {
    // Fetch TURN credentials
    List<Map<String, dynamic>> iceServers = [
      {'urls': 'stun:stun.l.google.com:19302'},
    ];

    try {
      // You can fetch from your backend here if needed
      // For now using Google STUN only (works on WiFi)
    } catch (_) {}

    final config = {
      'iceServers': iceServers,
      'sdpSemantics': 'unified-plan',
    };

    _peerConnection = await createPeerConnection(config);

    // Get microphone audio
    _localStream = await navigator.mediaDevices.getUserMedia({
      'audio': true,
      'video': false,
    });

    // Add local audio track to peer connection
    for (final track in _localStream!.getAudioTracks()) {
      await _peerConnection!.addTrack(track, _localStream!);
    }

    // Send ICE candidates to scanner via signalling server
    _peerConnection!.onIceCandidate = (candidate) {
      widget.wsChannel.sink.add(jsonEncode({
        'type':      'ice',
        'candidate': candidate.toMap(),
      }));
    };

    // Connection state changes
    _peerConnection!.onConnectionState = (state) {
      if (state == RTCPeerConnectionState.RTCPeerConnectionStateConnected) {
        // Call is live — start timer
        _startTimer();
      } else if (
        state == RTCPeerConnectionState.RTCPeerConnectionStateFailed ||
        state == RTCPeerConnectionState.RTCPeerConnectionStateDisconnected
      ) {
        _onCallEnded();
      }
    };
  }

  // ── Handle SDP offer from scanner ──
  Future<void> _handleOffer(String sdp) async {
    if (_peerConnection == null) return;

    await _peerConnection!.setRemoteDescription(
      RTCSessionDescription(sdp, 'offer'),
    );
    _remoteDescSet = true;

    // Add any queued ICE candidates
    for (final c in _pendingCandidates) {
      await _peerConnection!.addCandidate(c);
    }
    _pendingCandidates.clear();

    // Create answer
    final answer = await _peerConnection!.createAnswer();
    await _peerConnection!.setLocalDescription(answer);

    // Send answer to scanner
    widget.wsChannel.sink.add(jsonEncode({
      'type': 'answer',
      'sdp':  answer.sdp,
    }));
  }

  // ── Handle ICE candidate from scanner ──
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

  // ── Start call timer ──
  void _startTimer() {
    _callTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() => _seconds++);
    });
  }

  // ── End call ──
  void _endCall() {
    widget.wsChannel.sink.add(jsonEncode({'type': 'end'}));
    _onCallEnded();
  }

  // ── Call ended (by either side) ──
  void _onCallEnded() {
    if (_callEnded) return;
    setState(() => _callEnded = true);
    _callTimer?.cancel();
    _localStream?.dispose();
    _peerConnection?.close();
    if (mounted) Navigator.pop(context);
  }

  // ── Toggle mic ──
  void _toggleMic() {
    if (_localStream == null) return;
    final enabled = !_micMuted;
    for (final track in _localStream!.getAudioTracks()) {
      track.enabled = enabled;
    }
    setState(() => _micMuted = !_micMuted);
  }

  // ── Format timer ──
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

  // ── Incoming call UI ──
  Widget _buildIncomingCall() {
    return Padding(
      padding: const EdgeInsets.all(32),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          // Pulsing icon
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
          const Text('Incoming Call', style: TextStyle(
            color: Color(0xFF9CA3AF), fontSize: 14, letterSpacing: 1,
          )),
          const SizedBox(height: 8),
          const Text('Scanner', style: TextStyle(
            color: Colors.white, fontSize: 28, fontWeight: FontWeight.w800,
          )),
          const SizedBox(height: 6),
          Text('from sticker ${widget.stickerId}', style: const TextStyle(
            color: Color(0xFF6B7280), fontSize: 14,
          )),
          const SizedBox(height: 60),
          // Accept / Reject buttons
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              // Reject
              GestureDetector(
                onTap: _rejectCall,
                child: Container(
                  width: 70, height: 70,
                  decoration: const BoxDecoration(
                    color: Color(0xFFEF4444),
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(Icons.call_end, color: Colors.white, size: 30),
                ),
              ),
              const SizedBox(width: 48),
              // Accept
              GestureDetector(
                onTap: _acceptCall,
                child: Container(
                  width: 70, height: 70,
                  decoration: const BoxDecoration(
                    color: Color(0xFF22C55E),
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(Icons.call, color: Colors.white, size: 30),
                ),
              ),
            ],
          ),
          const SizedBox(height: 32),
          const Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(Icons.swipe_up, color: Color(0xFF374151), size: 16),
              SizedBox(width: 6),
              Text('Tap green to accept', style: TextStyle(
                color: Color(0xFF374151), fontSize: 13,
              )),
            ],
          ),
        ],
      ),
    );
  }

  // ── Active call UI ──
  Widget _buildActiveCall() {
    return Padding(
      padding: const EdgeInsets.all(32),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          // Avatar
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
          const Text('Scanner', style: TextStyle(
            color: Colors.white, fontSize: 26, fontWeight: FontWeight.w700,
          )),
          const SizedBox(height: 8),
          // Timer
          Text(_timerString, style: const TextStyle(
            color: Color(0xFF22C55E), fontSize: 20, fontWeight: FontWeight.w600,
            fontFeatures: [FontFeature.tabularFigures()],
          )),
          const SizedBox(height: 8),
          const Text('Call in progress', style: TextStyle(
            color: Color(0xFF6B7280), fontSize: 13,
          )),
          const SizedBox(height: 64),
          // Controls
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              // Mute button
              GestureDetector(
                onTap: _toggleMic,
                child: Container(
                  width: 60, height: 60,
                  decoration: BoxDecoration(
                    color: _micMuted
                        ? const Color(0xFFEF4444)
                        : const Color(0xFF1E2230),
                    shape: BoxShape.circle,
                    border: Border.all(
                      color: _micMuted
                          ? const Color(0xFFEF4444)
                          : const Color(0xFF232840),
                    ),
                  ),
                  child: Icon(
                    _micMuted ? Icons.mic_off : Icons.mic,
                    color: Colors.white, size: 24,
                  ),
                ),
              ),
              const SizedBox(width: 40),
              // End call button
              GestureDetector(
                onTap: _endCall,
                child: Container(
                  width: 72, height: 72,
                  decoration: const BoxDecoration(
                    color: Color(0xFFEF4444),
                    shape: BoxShape.circle,
                  ),
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