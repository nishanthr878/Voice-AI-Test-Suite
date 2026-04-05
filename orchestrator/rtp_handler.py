import socket
import threading
import time
import struct
import numpy as np
from typing import Callable, Optional


class RTPPacket:
    """
    Parses and builds RTP packets.
    RTP header is 12 bytes fixed.
    """

    HEADER_SIZE = 12

    def __init__(
        self,
        payload: bytes,
        sequence_number: int = 0,
        timestamp: int = 0,
        ssrc: int = 12345,
        payload_type: int = 0  # 0 = PCMU (G.711 ulaw)
    ):
        self.payload = payload
        self.sequence_number = sequence_number
        self.timestamp = timestamp
        self.ssrc = ssrc
        self.payload_type = payload_type

    @classmethod
    def parse(cls, data: bytes) -> Optional["RTPPacket"]:
        """
        Parse raw UDP bytes into RTPPacket.
        Returns None if data is too short to be valid RTP.
        """
        if len(data) < cls.HEADER_SIZE:
            return None

        # RTP header structure (12 bytes):
        # 0      : V(2) P(1) X(1) CC(4)
        # 1      : M(1) PT(7)
        # 2-3    : Sequence Number
        # 4-7    : Timestamp
        # 8-11   : SSRC
        # 12+    : Payload

        header = struct.unpack("!BBHII", data[:cls.HEADER_SIZE])
        payload_type = header[1] & 0x7F
        sequence_number = header[2]
        timestamp = header[3]
        ssrc = header[4]
        payload = data[cls.HEADER_SIZE:]

        return cls(
            payload=payload,
            sequence_number=sequence_number,
            timestamp=timestamp,
            ssrc=ssrc,
            payload_type=payload_type
        )

    def to_bytes(self) -> bytes:
        """Serialize RTPPacket to raw bytes for sending."""
        # Version=2, Padding=0, Extension=0, CC=0
        first_byte = 0x80
        # Marker=0, PayloadType
        second_byte = self.payload_type & 0x7F

        header = struct.pack(
            "!BBHII",
            first_byte,
            second_byte,
            self.sequence_number,
            self.timestamp,
            self.ssrc
        )
        return header + self.payload

    @staticmethod
    def pcm_to_ulaw(pcm_bytes: bytes) -> bytes:
        """
        Convert 16-bit PCM to G.711 ulaw (PCMU).
        Genesys expects PCMU payload in RTP packets.
        """
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        ulaw = []

        for sample in samples:
            # Bias
            sample = np.clip(sample, -32768, 32767)
            sign = 0 if sample >= 0 else 0x80
            sample = abs(sample)
            sample = min(sample + 132, 32767)

            # Find segment
            exp = 7
            for exp in range(7, -1, -1):
                if sample >= (1 << (exp + 3)):
                    break

            mantissa = (sample >> (exp + 3)) & 0x0F
            ulaw_byte = ~(sign | (exp << 4) | mantissa) & 0xFF
            ulaw.append(ulaw_byte)

        return bytes(ulaw)

    @staticmethod
    def ulaw_to_pcm(ulaw_bytes: bytes) -> bytes:
        """
        Convert G.711 ulaw to 16-bit PCM.
        Incoming RTP from Genesys is PCMU — decode to PCM for Whisper.
        """
        pcm = []
        for byte in ulaw_bytes:
            byte = ~byte & 0xFF
            sign = byte & 0x80
            exp = (byte >> 4) & 0x07
            mantissa = byte & 0x0F
            sample = ((mantissa << 1) | 1) << (exp + 2)
            if sign:
                sample = -sample
            pcm.append(max(-32768, min(32767, sample)))

        return np.array(pcm, dtype=np.int16).tobytes()


class RTPHandler:
    """
    Manages bidirectional RTP audio stream.

    Receiving: captures RTP packets from Genesys,
               decodes PCMU → PCM,
               buffers audio,
               fires callback when utterance complete.

    Sending: takes PCM audio from TTS,
             encodes PCM → PCMU,
             sends RTP packets to Genesys at 20ms intervals.
    """

    # RTP timing constants for G.711 at 8kHz
    PACKET_DURATION_MS = 20          # 20ms per RTP packet
    SAMPLES_PER_PACKET = 160         # 8000Hz * 0.02s = 160 samples
    BYTES_PER_PACKET = 160           # ulaw = 1 byte per sample
    TIMESTAMP_INCREMENT = 160        # RTP timestamp units

    def __init__(
        self,
        local_port: int,
        remote_ip: str,
        remote_port: int,
        on_utterance_complete: Callable[[bytes], None]
    ):
        """
        Args:
            local_port: UDP port we listen on for incoming RTP
            remote_ip: Genesys RTP IP to send audio to
            remote_port: Genesys RTP port to send audio to
            on_utterance_complete: callback fired with raw PCM bytes
                                   when voice AI finishes speaking
        """
        self.local_port = local_port
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self.on_utterance_complete = on_utterance_complete

        # UDP socket for RTP
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("0.0.0.0", local_port))
        self.socket.settimeout(1.0)  # 1s timeout for clean shutdown

        # State
        self.running = False
        self.sequence_number = 0
        self.timestamp = 0
        self.ssrc = 12345

        # Audio buffer for utterance detection
        self.audio_buffer = bytearray()
        self.silent_packets = 0
        self.is_speaking = False

        # Silence detection
        # At 20ms/packet: 40 packets = 800ms silence
        self.silence_packet_limit = 40
        self.silence_threshold = 100  # ulaw amplitude threshold

        # Threading
        self._recv_thread = None
        self._send_lock = threading.Lock()

        print(f"[RTP] Handler initialized on port {local_port}")
        print(f"[RTP] Remote: {remote_ip}:{remote_port}")

    def start(self):
        """Start receiving RTP packets in background thread."""
        self.running = True
        self._recv_thread = threading.Thread(
            target=self._receive_loop,
            daemon=True
        )
        self._recv_thread.start()
        print("[RTP] Receive loop started")

    def stop(self):
        """Stop RTP handler and close socket."""
        self.running = False
        if self._recv_thread:
            self._recv_thread.join(timeout=2.0)
        self.socket.close()
        print("[RTP] Handler stopped")

    def send_audio(self, pcm_bytes: bytes):
        """
        Send PCM audio to Genesys over RTP.
        Encodes PCM → PCMU and sends in 20ms packets.

        Called by orchestrator after TTS generates audio.
        """
        if not pcm_bytes:
            return

        with self._send_lock:
            # Convert PCM to ulaw for RTP
            ulaw_audio = RTPPacket.pcm_to_ulaw(pcm_bytes)

            # Send in 160-byte chunks (20ms each)
            for i in range(0, len(ulaw_audio), self.BYTES_PER_PACKET):
                chunk = ulaw_audio[i:i + self.BYTES_PER_PACKET]

                # Pad last packet if needed
                if len(chunk) < self.BYTES_PER_PACKET:
                    chunk = chunk + b'\x7F' * (
                        self.BYTES_PER_PACKET - len(chunk)
                    )

                packet = RTPPacket(
                    payload=chunk,
                    sequence_number=self.sequence_number,
                    timestamp=self.timestamp,
                    ssrc=self.ssrc,
                    payload_type=0  # PCMU
                )

                self.socket.sendto(
                    packet.to_bytes(),
                    (self.remote_ip, self.remote_port)
                )

                self.sequence_number = (self.sequence_number + 1) % 65536
                self.timestamp += self.TIMESTAMP_INCREMENT

                # Pace packets at 20ms intervals — real phone timing
                time.sleep(self.PACKET_DURATION_MS / 1000)

        print(f"[RTP] Sent {len(ulaw_audio)} bytes of audio")

    def send_silence(self, duration_ms: int):
        """
        Send comfort noise during processing gaps.
        Prevents Genesys from thinking the call dropped.
        """
        num_packets = duration_ms // self.PACKET_DURATION_MS
        silence_payload = b'\x7F' * self.BYTES_PER_PACKET  # ulaw silence

        with self._send_lock:
            for _ in range(num_packets):
                packet = RTPPacket(
                    payload=silence_payload,
                    sequence_number=self.sequence_number,
                    timestamp=self.timestamp,
                    ssrc=self.ssrc,
                    payload_type=0
                )
                self.socket.sendto(
                    packet.to_bytes(),
                    (self.remote_ip, self.remote_port)
                )
                self.sequence_number = (self.sequence_number + 1) % 65536
                self.timestamp += self.TIMESTAMP_INCREMENT
                time.sleep(self.PACKET_DURATION_MS / 1000)

    def _receive_loop(self):
        """
        Background thread that continuously receives RTP packets.
        Detects utterances via silence detection.
        Fires on_utterance_complete callback with raw PCM.
        """
        print("[RTP] Listening for audio...")

        while self.running:
            try:
                data, addr = self.socket.recvfrom(4096)
                packet = RTPPacket.parse(data)

                if packet is None:
                    continue

                # Decode ulaw → PCM
                pcm_chunk = RTPPacket.ulaw_to_pcm(packet.payload)

                # Fire callback with every chunk
                # UtteranceBuffer in main.py handles silence detection
                self.on_utterance_complete(pcm_chunk)

                # Silence detection on ulaw payload directly
                is_silent = self._is_silent(packet.payload)

                if not is_silent:
                    self.audio_buffer.extend(pcm_chunk)
                    self.silent_packets = 0
                    self.is_speaking = True

                elif self.is_speaking:
                    # Was speaking, now silent
                    self.audio_buffer.extend(pcm_chunk)
                    self.silent_packets += 1

                    if self.silent_packets >= self.silence_packet_limit:
                        # 800ms of silence — utterance complete
                        audio_data = bytes(self.audio_buffer)
                        self._reset_buffer()

                        print(
                            f"[RTP] Utterance complete: "
                            f"{len(audio_data)} bytes"
                        )

                        # Fire callback with raw PCM
                        self.on_utterance_complete(audio_data)

            except socket.timeout:
                # Normal — just loop again
                continue
            except Exception as e:
                if self.running:
                    print(f"[RTP] Receive error: {e}")

    def _is_silent(self, ulaw_bytes: bytes) -> bool:
        """
        Detect silence directly on ulaw payload.
        ulaw 0x7F = silence value.
        """
        if not ulaw_bytes:
            return True

        # Calculate average deviation from ulaw silence value (0x7F)
        samples = np.frombuffer(ulaw_bytes, dtype=np.uint8)
        avg_amplitude = np.mean(np.abs(samples.astype(int) - 0x7F))

        return avg_amplitude < self.silence_threshold

    def _reset_buffer(self):
        """Reset audio buffer for next utterance."""
        self.audio_buffer = bytearray()
        self.silent_packets = 0
        self.is_speaking = False