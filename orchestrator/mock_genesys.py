"""
Mock Genesys for local testing.
Simulates the full Genesys flow:
1. Receives outbound call trigger (HTTP)
2. Sends SIP INVITE to your SIP server
3. Streams mock voice AI audio over RTP
4. Responds to BYE

Run this alongside the main orchestrator:
    python mock_genesys.py
"""

import socket
import time
import threading
import struct
import numpy as np
import subprocess
import os
import requests


# ----------------------------------------------------------------
# Config — must match your .env
# ----------------------------------------------------------------

LOCAL_IP = "127.0.0.1"
LOCAL_SIP_PORT = 5061        # mock Genesys SIP port
LOCAL_RTP_PORT = 20000       # mock Genesys RTP port

TARGET_SIP_IP = "127.0.0.1"
TARGET_SIP_PORT = 5060       # your SIP server port
TARGET_RTP_PORT = 10000      # your RTP port

# Mock conversation — what the voice AI says each turn
MOCK_CONVERSATION = [
    "Hello, thank you for calling. How can I help you today?",
    "Sure, can I get your order number please?",
    "I can see order ORD-789456, Blue Wireless Headphones, "
    "quantity 1. What is the reason for your return?",
    "I understand. I have sent a verification email to your "
    "address. Please click the link to confirm.",
    "Your return for order ORD-789456 has been successfully "
    "initiated. Is there anything else I can help you with?",
    "Thank you for calling. Goodbye."
]


# ----------------------------------------------------------------
# Text to audio using espeak (fallback if Piper not available)
# ----------------------------------------------------------------

def text_to_pcm(text: str) -> bytes:
    """Convert text to raw PCM at 8kHz mono using espeak."""
    print(f"[MOCK] Synthesizing: {text}")

    process = subprocess.run(
        [
            "espeak",
            "-v", "en",
            "-s", "130",
            "-a", "180",
            "--stdout",
            text
        ],
        capture_output=True,
        timeout=10
    )

    if process.returncode != 0 or not process.stdout:
        print("[MOCK] espeak failed — using tone")
        return _generate_tone(duration_ms=max(1000, len(text) * 60))

    # espeak outputs WAV — convert to raw PCM at 8kHz using ffmpeg
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(process.stdout)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-i", tmp_path,
                "-ar", "8000",   # resample to 8kHz
                "-ac", "1",      # mono
                "-f", "s16le",   # raw PCM
                "-",
                "-loglevel", "error"
            ],
            capture_output=True,
            timeout=10
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
        else:
            return _generate_tone(duration_ms=max(1000, len(text) * 60))
    finally:
        os.unlink(tmp_path)


def _wav_to_pcm_raw(wav_bytes: bytes) -> bytes:
    """Strip WAV header and return raw PCM bytes."""
    # WAV header is 44 bytes
    if len(wav_bytes) > 44:
        return wav_bytes[44:]
    return wav_bytes


def _generate_tone(
    duration_ms: int = 2000,
    frequency: int = 440,
    sample_rate: int = 8000
) -> bytes:
    """
    Generate a sine wave tone as fallback audio.
    Used when espeak is not available.
    """
    num_samples = int(sample_rate * duration_ms / 1000)
    t = np.linspace(0, duration_ms / 1000, num_samples)
    tone = (np.sin(2 * np.pi * frequency * t) * 16000).astype(np.int16)
    return tone.tobytes()


# ----------------------------------------------------------------
# RTP sender
# ----------------------------------------------------------------

def send_rtp_audio(
    pcm_bytes: bytes,
    target_ip: str,
    target_port: int
):
    """
    Send PCM audio as RTP packets to target.
    Simulates voice AI speaking.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    sequence_number = 0
    timestamp = 0
    ssrc = 99999

    # Convert PCM to ulaw
    ulaw_audio = _pcm_to_ulaw(pcm_bytes)

    print(
        f"[MOCK RTP] Sending {len(ulaw_audio)} bytes "
        f"to {target_ip}:{target_port}"
    )

    # Send in 160-byte packets (20ms each)
    for i in range(0, len(ulaw_audio), 160):
        chunk = ulaw_audio[i:i + 160]

        if len(chunk) < 160:
            chunk = chunk + b'\x7F' * (160 - len(chunk))

        # Build RTP header
        header = struct.pack(
            "!BBHII",
            0x80,              # V=2, P=0, X=0, CC=0
            0x00,              # M=0, PT=0 (PCMU)
            sequence_number,
            timestamp,
            ssrc
        )

        sock.sendto(header + chunk, (target_ip, target_port))

        sequence_number = (sequence_number + 1) % 65536
        timestamp += 160

        # 20ms pacing
        time.sleep(0.02)

    sock.close()
    print("[MOCK RTP] Audio sent")


def _pcm_to_ulaw(pcm_bytes: bytes) -> bytes:
    """Convert 16-bit PCM to G.711 ulaw."""
    # First resample to 8kHz if needed
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    
    # Resample from espeak rate (22050Hz) to 8kHz
    target_length = int(len(audio) * 8000 / 22050)
    resampled = np.interp(
        np.linspace(0, len(audio) - 1, target_length),
        np.arange(len(audio)),
        audio
    ).astype(np.int16)
    
    # Now encode to ulaw
    ulaw = []
    for sample in resampled:
        sample = int(np.clip(sample, -32768, 32767))
        sign = 0 if sample >= 0 else 0x80
        sample = abs(sample)
        sample = min(sample + 132, 32767)
        exp = 7
        for exp in range(7, -1, -1):
            if sample >= (1 << (exp + 3)):
                break
        mantissa = (sample >> (exp + 3)) & 0x0F
        ulaw_byte = ~(sign | (exp << 4) | mantissa) & 0xFF
        ulaw.append(ulaw_byte)
    
    return bytes(ulaw)


# ----------------------------------------------------------------
# SIP mock
# ----------------------------------------------------------------

class MockGenesysSIP:
    """
    Simulates Genesys SIP behavior.
    Sends INVITE to your SIP server and handles the call.
    """

    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("0.0.0.0", LOCAL_SIP_PORT))
        self.socket.settimeout(30)

        self.call_id = "mock-call-12345@genesys"
        self.call_active = False
        self.target_rtp_port = None  # extracted from 200 OK SDP

    def send_invite(self):
        """Send SIP INVITE to your SIP server."""
        sdp = (
            f"v=0\r\n"
            f"o=genesys 53655765 2353687637 IN IP4 {LOCAL_IP}\r\n"
            f"s=Mock Genesys\r\n"
            f"c=IN IP4 {LOCAL_IP}\r\n"
            f"t=0 0\r\n"
            f"m=audio {LOCAL_RTP_PORT} RTP/AVP 0 8\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=rtpmap:8 PCMA/8000\r\n"
            f"a=ptime:20\r\n"
        )

        invite = (
            f"INVITE sip:voiceai-tester@{TARGET_SIP_IP}:{TARGET_SIP_PORT}"
            f" SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {LOCAL_IP}:{LOCAL_SIP_PORT}"
            f";branch=z9hG4bKmock123\r\n"
            f"From: <sip:genesys@{LOCAL_IP}>;tag=genesys-tag\r\n"
            f"To: <sip:voiceai-tester@{TARGET_SIP_IP}>\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: 1 INVITE\r\n"
            f"Contact: <sip:genesys@{LOCAL_IP}:{LOCAL_SIP_PORT}>\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp)}\r\n"
            f"\r\n"
            f"{sdp}"
        )

        print(f"[MOCK SIP] Sending INVITE to "
              f"{TARGET_SIP_IP}:{TARGET_SIP_PORT}")

        self.socket.sendto(
            invite.encode(),
            (TARGET_SIP_IP, TARGET_SIP_PORT)
        )

    def wait_for_ok(self) -> bool:
        """
        Wait for 200 OK from your SIP server.
        Extracts RTP port from SDP for audio streaming.
        """
        print("[MOCK SIP] Waiting for 200 OK...")

        while True:
            try:
                data, addr = self.socket.recvfrom(4096)
                raw = data.decode("utf-8", errors="ignore")

                if "200 OK" in raw:
                    print("[MOCK SIP] 200 OK received")

                    # Extract target RTP port from SDP
                    for line in raw.split("\r\n"):
                        if line.startswith("m=audio"):
                            parts = line.split()
                            if len(parts) >= 2:
                                self.target_rtp_port = int(parts[1])
                                print(
                                    f"[MOCK SIP] Target RTP port: "
                                    f"{self.target_rtp_port}"
                                )

                    # Send ACK
                    self._send_ack()
                    self.call_active = True
                    return True

                elif "100" in raw or "180" in raw:
                    # Trying/Ringing — keep waiting
                    continue

            except socket.timeout:
                print("[MOCK SIP] Timeout waiting for 200 OK")
                return False

    def _send_ack(self):
        """Send ACK after 200 OK."""
        ack = (
            f"ACK sip:voiceai-tester@{TARGET_SIP_IP}:{TARGET_SIP_PORT}"
            f" SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {LOCAL_IP}:{LOCAL_SIP_PORT}"
            f";branch=z9hG4bKmock456\r\n"
            f"From: <sip:genesys@{LOCAL_IP}>;tag=genesys-tag\r\n"
            f"To: <sip:voiceai-tester@{TARGET_SIP_IP}>\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: 1 ACK\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n\r\n"
        )

        self.socket.sendto(ack.encode(), (TARGET_SIP_IP, TARGET_SIP_PORT))
        print("[MOCK SIP] ACK sent")

    def wait_for_bye(self):
        """
        Wait for BYE from your SIP server.
        Send 200 OK when received.
        """
        print("[MOCK SIP] Waiting for BYE...")
        self.socket.settimeout(120)

        try:
            while True:
                data, addr = self.socket.recvfrom(4096)
                raw = data.decode("utf-8", errors="ignore")

                if "BYE" in raw:
                    print("[MOCK SIP] BYE received — sending 200 OK")
                    ok = (
                        f"SIP/2.0 200 OK\r\n"
                        f"Via: SIP/2.0/UDP {TARGET_SIP_IP}:{TARGET_SIP_PORT}"
                        f";branch=z9hG4bKbye\r\n"
                        f"From: <sip:voiceai-tester@{TARGET_SIP_IP}>\r\n"
                        f"To: <sip:genesys@{LOCAL_IP}>\r\n"
                        f"Call-ID: {self.call_id}\r\n"
                        f"CSeq: 2 BYE\r\n"
                        f"Content-Length: 0\r\n\r\n"
                    )
                    self.socket.sendto(ok.encode(), addr)
                    self.call_active = False
                    return

        except socket.timeout:
            print("[MOCK SIP] Timeout waiting for BYE")


# ----------------------------------------------------------------
# Mock HTTP server — replaces Genesys outbound call API
# ----------------------------------------------------------------

from flask import Flask, jsonify, request as flask_request

mock_app = Flask(__name__)
call_triggered = threading.Event()


@mock_app.route(
    "/api/v2/conversations/calls",
    methods=["POST"]
)
def mock_trigger_call():
    """
    Mock Genesys outbound call API.
    When orchestrator hits this, we trigger the SIP flow.
    """
    print("[MOCK API] Outbound call triggered by orchestrator")
    call_triggered.set()

    return jsonify({
        "id": "mock-conversation-12345",
        "state": "connected"
    }), 201


@mock_app.route("/oauth/token", methods=["POST"])
def mock_token():
    """Mock OAuth2 token endpoint."""
    return jsonify({
        "access_token": "mock-token-12345",
        "token_type": "bearer",
        "expires_in": 3600
    })


# ----------------------------------------------------------------
# Main mock flow
# ----------------------------------------------------------------

def run_mock_api():
    """Run mock Genesys HTTP API on port 9000."""
    mock_app.run(host="0.0.0.0", port=9000, debug=False)


def run_mock_call():
    """
    Wait for orchestrator to trigger a call,
    then simulate the full Genesys SIP + RTP flow.
    """
    print("[MOCK] Waiting for orchestrator to trigger call...")
    call_triggered.wait()

    print("[MOCK] Call triggered — starting SIP flow")
    time.sleep(1)  # brief delay simulating Genesys processing

    sip = MockGenesysSIP()

    # Send INVITE
    sip.send_invite()

    # Wait for 200 OK
    if not sip.wait_for_ok():
        print("[MOCK] Failed to connect call")
        return

    target_rtp = sip.target_rtp_port or TARGET_RTP_PORT

    # Give SIP server time to start RTP handler
    time.sleep(1)

    print("[MOCK] Call connected — starting conversation")

    # Play through mock conversation
    for i, utterance in enumerate(MOCK_CONVERSATION):
        print(f"[MOCK] Voice AI turn {i + 1}: {utterance}")

        # Synthesize and send audio
        pcm = text_to_pcm(utterance)
        send_rtp_audio(pcm, TARGET_SIP_IP, target_rtp)

        # Wait for customer response audio
        # In real flow customer agent sends audio back
        # We just wait a few seconds here
        print(f"[MOCK] Waiting for customer response...")
        time.sleep(4)

    # Wait for BYE or send it ourselves
    print("[MOCK] Conversation complete — waiting for BYE")
    sip.wait_for_bye()
    print("[MOCK] Mock call complete")


if __name__ == "__main__":
    print("=" * 50)
    print("MOCK GENESYS SERVER")
    print("=" * 50)
    print("HTTP API : http://localhost:9000")
    print("SIP      : 0.0.0.0:5061")
    print("RTP      : 0.0.0.0:20000")
    print("=" * 50)

    # Run HTTP API in background
    api_thread = threading.Thread(target=run_mock_api, daemon=True)
    api_thread.start()

    # Run mock call flow in foreground
    run_mock_call()