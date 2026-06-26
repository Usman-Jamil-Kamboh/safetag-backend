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
  bool _speakerOn    = false; // default: earpiece (not speaker)

  RTCPeerConnection? _peerConnection;
  MediaStream? _localStream;

  int _seconds = 0;
  Timer? _callTimer;

  final List<RTCIceCandidate> _pendingCandidates = [];
  bool _remoteDescSet = false;
  String? _pendingOfferSdp; // offer that arrived before Accept was tapped

  @override
  void initState() {
    super.initState();
    print('[CallScreen] OPENED — isIncoming=${widget.isIncoming}');
    // Wire signal handler to this screen
    widget.wsService.onMessage = _handleSignal;
  }

  @override
  void dispose() {
    print('[CallScreen] DISPOSED (closing)');
    _callTimer?.cancel();
    _localStream?.dispose();
    _peerConnection?.close();
    // Restore signal handler to history screen callbacks
    // (HistoryScreen will re-register when it comes back)
    super.dispose();
  }

  void _handleSignal(Map<String, dynamic> msg) async {
    final type = msg['type'];
    print('[CallScreen] SIGNAL RECEIVED: $type  ->  $msg');
    if (type == 'offer') {
      if (_peerConnection == null) {
        // Accept hasn't been tapped yet — don't lose the offer, queue it.
        print('[CallScreen] Offer arrived before peerConnection ready — queuing');
        _pendingOfferSdp = msg['sdp'];
      } else {
        await _handleOffer(msg['sdp']);
      }
    }
    else if (type == 'ice')   await _handleIce(msg['candidate']);
    else if (type == 'end')   _onCallEnded();
  }

  bool _settingUp = false;

  Future<void> _acceptCall() async {
    if (_settingUp || _callAccepted) return; // ignore double-tap on Accept
    print('[CallScreen] Accept tapped — setting up WebRTC');
    _settingUp = true;
    setState(() => _callAccepted = true);
    await _setupWebRTC();
    print('[CallScreen] WebRTC setup done, waiting for offer/ice...');
    // If the offer arrived BEFORE we tapped Accept (very common — the
    // scanner sends it immediately when the call starts), process it now.
    if (_pendingOfferSdp != null) {
      print('[CallScreen] Processing OFFER that arrived before Accept');
      final sdp = _pendingOfferSdp!;
      _pendingOfferSdp = null;
      await _handleOffer(sdp);
    }
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
        // TURN relay — needed because pure STUN fails on most Pakistani
        // mobile networks (CGNAT). This is a FREE PUBLIC test TURN server
        // (openrelay.metered.ca) — fine for testing, but get your own
        // TURN (Metered.ca free tier / Twilio / self-hosted coturn) before
        // going to production, since this public one is rate-limited.
        {
          'urls': 'turn:openrelay.metered.ca:80',
          'username': 'openrelayproject',
          'credential': 'openrelayproject',
        },
        {
          'urls': 'turn:openrelay.metered.ca:443',
          'username': 'openrelayproject',
          'credential': 'openrelayproject',
        },
        {
          'urls': 'turn:openrelay.metered.ca:443?transport=tcp',
          'username': 'openrelayproject',
          'credential': 'openrelayproject',
        },
      ],
      'sdpSemantics': 'unified-plan',
    };

    _peerConnection = await createPeerConnection(config);

    _localStream = await navigator.mediaDevices.getUserMedia({
      'audio': true, 'video': false,
    });

    // Default to earpiece, not speaker — sounds like a normal phone call.
    // A toggle button lets the owner switch to speaker if they want.
    try {
      await Helper.setSpeakerphoneOn(_speakerOn);
    } catch (e) {
      print('[CallScreen] setSpeakerphoneOn failed: $e');
    }

    for (final track in _localStream!.getAudioTracks()) {
      await _peerConnection!.addTrack(track, _localStream!);
    }

    _peerConnection!.onIceCandidate = (candidate) {
      print('[CallScreen] Sending our ICE candidate to scanner');
      widget.wsService.sendMessage({'type': 'ice', 'candidate': candidate.toMap()});
    };

    _peerConnection!.onIceConnectionState = (state) {
      // ignore: avoid_print
      print('[CallScreen] ICE connection state: $state');
    };

    _peerConnection!.onConnectionState = (state) {
      // ignore: avoid_print
      print('[CallScreen] Peer connection state: $state');
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
    print('[CallScreen] Handling OFFER, peerConnection null? ${_peerConnection == null}');
    if (_peerConnection == null) return;
    await _peerConnection!.setRemoteDescription(RTCSessionDescription(sdp, 'offer'));
    _remoteDescSet = true;
    print('[CallScreen] Remote description (offer) set. Pending candidates: ${_pendingCandidates.length}');
    for (final c in _pendingCandidates) {
      await _peerConnection!.addCandidate(c);
    }
    _pendingCandidates.clear();
    final answer = await _peerConnection!.createAnswer();
    await _peerConnection!.setLocalDescription(answer);
    print('[CallScreen] Sending ANSWER back to scanner');
    widget.wsService.sendMessage({'type': 'answer', 'sdp': answer.sdp});
  }

  Future<void> _handleIce(Map<String, dynamic> candidateMap) async {
    final candidate = RTCIceCandidate(
      candidateMap['candidate'],
      candidateMap['sdpMid'],
      candidateMap['sdpMLineIndex'],
    );
    if (_remoteDescSet && _peerConnection != null) {
      print('[CallScreen] Adding ICE candidate immediately');
      await _peerConnection!.addCandidate(candidate);
    } else {
      print('[CallScreen] Queuing ICE candidate (remote desc not set yet)');
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
    print('[CallScreen] _onCallEnded() called');
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

  void _toggleSpeaker() async {
    final newValue = !_speakerOn;
    try {
      await Helper.setSpeakerphoneOn(newValue);
      setState(() => _speakerOn = newValue);
    } catch (e) {
      print('[CallScreen] setSpeakerphoneOn failed: $e');
    }
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
              const SizedBox(width: 40),
              GestureDetector(
                onTap: _toggleSpeaker,
                child: Container(
                  width: 60, height: 60,
                  decoration: BoxDecoration(
                    color: _speakerOn ? const Color(0xFF3B82F6) : const Color(0xFF1E2230),
                    shape: BoxShape.circle,
                    border: Border.all(
                      color: _speakerOn ? const Color(0xFF3B82F6) : const Color(0xFF232840),
                    ),
                  ),
                  child: Icon(
                    _speakerOn ? Icons.volume_up : Icons.hearing,
                    color: Colors.white, size: 24,
                  ),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}