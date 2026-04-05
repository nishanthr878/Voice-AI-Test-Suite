import os
import io
import numpy as np
from groq import Groq
from dotenv import load_dotenv

load_dotenv()


class STTEngine:
    def __init__(self):
        self.client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        self.sample_rate = 8000
        self.silence_threshold = 0.001
        print("[STT] Groq Whisper STT ready")

    def transcribe(self, audio_bytes: bytes) -> str:
        if not audio_bytes:
            return ""

        duration_ms = (len(audio_bytes) / 16000) * 1000
        print(f"[STT] Transcribing {len(audio_bytes)} bytes ({duration_ms:.0f}ms)")

        try:
            # Convert raw PCM to WAV for Groq API
            wav_bytes = self._pcm_to_wav(audio_bytes)

            transcription = self.client.audio.transcriptions.create(
                file=("audio.wav", wav_bytes, "audio/wav"),
                model="whisper-large-v3-turbo",
                language="en",
                response_format="text"
            )

            text = str(transcription).strip()
            if text:
                print(f"[STT] Transcribed: '{text}'")
            else:
                print("[STT] No speech detected")
            return text

        except Exception as e:
            print(f"[STT] Transcription error: {e}")
            return ""

    def is_silent(self, audio_chunk: bytes) -> bool:
        if not audio_chunk:
            return True
        audio_array = np.frombuffer(audio_chunk, dtype=np.int16)
        audio_float = audio_array.astype(np.float32) / 32768.0
        rms = np.sqrt(np.mean(audio_float ** 2))
        return rms < self.silence_threshold

    def _pcm_to_wav(self, pcm_bytes: bytes) -> bytes:
        """Convert raw 8kHz 16-bit mono PCM to WAV bytes."""
        import wave
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(pcm_bytes)
        buf.seek(0)
        return buf.read()


class UtteranceBuffer:
    def __init__(self, stt: STTEngine):
        self.stt = stt
        self.buffer = bytearray()
        self.silent_chunks = 0
        self.silence_chunk_limit = 80  # 80 * 20ms = 1.6 seconds silence
        self.min_speech_chunks = 10    # need at least 200ms of speech
        self.speech_chunks = 0
        self.is_speaking = False

    def add_chunk(self, chunk: bytes) -> str | None:
        silent = self.stt.is_silent(chunk)

        if not silent:
            self.buffer.extend(chunk)
            self.silent_chunks = 0
            self.speech_chunks += 1
            self.is_speaking = True

        elif self.is_speaking:
            self.buffer.extend(chunk)
            self.silent_chunks += 1

            if self.silent_chunks >= self.silence_chunk_limit:
                # Only transcribe if we had enough speech
                if self.speech_chunks >= self.min_speech_chunks:
                    audio_data = bytes(self.buffer)
                    self._reset()
                    print(f"[STT] Utterance complete: {len(audio_data)} bytes")
                    text = self.stt.transcribe(audio_data)
                    return text if text else None
                else:
                    # Too short — likely noise, discard
                    print(f"[STT] Discarding noise: {self.speech_chunks} chunks")
                    self._reset()

        return None

    def _reset(self):
        self.buffer = bytearray()
        self.silent_chunks = 0
        self.speech_chunks = 0
        self.is_speaking = False

    def flush(self) -> str | None:
        if self.buffer:
            audio_data = bytes(self.buffer)
            self._reset()
            text = self.stt.transcribe(audio_data)
            return text if text else None
        return None