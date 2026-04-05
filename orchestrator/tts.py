import os
import io
import numpy as np
from dotenv import load_dotenv

load_dotenv()


class TTSEngine:
    def __init__(self):
        self.model_path = os.getenv(
            "PIPER_MODEL",
            "/app/models/en_US-lessac-medium.onnx"
        )
        self.voice = None
        self._load_model()

    def _load_model(self):
        """Load Piper voice model."""
        try:
            from piper import PiperVoice
            if os.path.exists(self.model_path):
                print(f"[TTS] Loading Piper model: {self.model_path}")
                self.voice = PiperVoice.load(self.model_path)
                print("[TTS] Piper model loaded")
            else:
                print("[TTS] Piper model not found — using espeak fallback")
        except ImportError:
            print("[TTS] Piper not installed — using espeak fallback")

    def synthesize(self, text: str) -> bytes:
        """Convert text to raw PCM bytes at 8kHz mono."""
        if not text or not text.strip():
            return b""

        print(f"[TTS] Synthesizing: {text}")

        if self.voice:
            return self._piper_synthesize(text)
        return self._espeak_synthesize(text)

    def _piper_synthesize(self, text: str) -> bytes:
        import wave
        import tempfile

        # Write to temp file instead of BytesIO — more reliable with Piper
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name

        try:
            with wave.open(tmp_path, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(22050)
                self.voice.synthesize(text, wav)

            # Read back and check size
            with open(tmp_path, 'rb') as f:
                wav_data = f.read()

            print(f"[TTS] WAV file size: {len(wav_data)} bytes")

            if len(wav_data) <= 44:
                print("[TTS] Piper produced empty audio — falling back to espeak")
                return self._espeak_synthesize(text)

            # Strip WAV header, resample to 8kHz
            pcm = wav_data[44:]
            return self._resample(pcm, 22050, 8000)

        finally:
            import os
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _espeak_synthesize(self, text: str) -> bytes:
        """Fallback TTS using espeak."""
        import subprocess
        print("[TTS] Using espeak fallback")
        process = subprocess.run(
            ["espeak", "-v", "en", "-s", "150", "--stdout", text],
            capture_output=True,
            timeout=10
        )
        if process.returncode != 0 or not process.stdout:
            return b'\x7F' * 16000  # 1 second silence
        return process.stdout[44:]  # strip WAV header

    def _resample(
        self,
        pcm_bytes: bytes,
        orig_rate: int,
        target_rate: int
    ) -> bytes:
        """Resample PCM audio from orig_rate to target_rate."""
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
        target_length = int(len(audio) * target_rate / orig_rate)
        resampled = np.interp(
            np.linspace(0, len(audio) - 1, target_length),
            np.arange(len(audio)),
            audio
        ).astype(np.int16)
        return resampled.tobytes()

    def synthesize_chunks(self, text: str, chunk_size: int = 320):
        """Yield audio in 20ms RTP-sized chunks."""
        audio = self.synthesize(text)
        for i in range(0, len(audio), chunk_size):
            yield audio[i:i + chunk_size]

    def get_duration_ms(self, pcm_bytes: bytes) -> int:
        """Duration of PCM audio in milliseconds at 8kHz 16-bit."""
        return (len(pcm_bytes) / 16000) * 1000