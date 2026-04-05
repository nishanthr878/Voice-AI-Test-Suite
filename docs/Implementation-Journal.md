# Voice AI Testing Suite — Implementation Journal

## What We Built

An automated voice AI testing suite that replaces manual call testing. A synthetic AI customer calls a target voice AI via SIP, conducts a full conversation, and produces structured pass/fail results with transcripts and scores.

---

## Final Architecture

```
Orchestrator (Python/Flask) — Single Docker Container
├── genesys.py       → OAuth2 auth + outbound call trigger
├── sip_server.py    → Accepts SIP INVITE from Genesys
├── rtp_handler.py   → Bidirectional RTP audio (G.711 ulaw)
├── stt.py           → Groq Whisper STT (speech → text)
├── tts.py           → Piper TTS with espeak fallback (text → speech)
├── agent.py         → Groq/Llama 3.3 70B customer agent
├── evaluator.py     → Rules engine + LLM judge (pass/fail)
├── logger.py        → JSON log + plain text transcript
├── main.py          → Flask API + call orchestration
└── mock_genesys.py  → Mock Genesys for local testing
```

---

## Tech Stack

| Component | Tool | Reason |
|---|---|---|
| SIP | Custom Python SIP server | FreeSWITCH packaging failed on all platforms |
| RTP | Raw UDP sockets (G.711 ulaw) | No external dependency needed |
| STT | Groq Whisper API (whisper-large-v3-turbo) | Local Whisper too slow on CPU |
| TTS | Piper TTS + espeak fallback | Local, open source, no API key |
| LLM | Groq API (llama-3.3-70b-versatile) | Fast, free tier, high quality |
| Framework | Flask | Simpler than FastAPI for POC |
| Infrastructure | Docker (single container) | Portable, no environment issues |

---

## Key Design Decisions

### Why not FreeSWITCH?
FreeSWITCH was the original plan. Abandoned because:
- SignalWire repo requires paid auth token
- Ubuntu apt repo doesn't have FreeSWITCH packages
- Docker images are either dead or behind paywall
- Replaced with a custom Python SIP server — simpler, no external deps

### Why not Twilio/Deepgram/ElevenLabs?
- Twilio: US call restrictions + licensing constraints
- Deepgram/ElevenLabs: paid APIs, not available in testing environment
- Replaced with Groq free tier (STT + LLM) and Piper TTS (local)

### Why Groq over local Ollama?
- Groq runs Llama 3.3 70B at ~500 tokens/second
- No GPU required on developer machine
- Free tier sufficient for POC volume
- Local Llama via Ollama requires 48GB+ VRAM for 70B quality

### SIP Architecture
Instead of FreeSWITCH ESL, we wrote a minimal SIP server in Python:
- Listens on UDP port 5060
- Accepts INVITE from Genesys
- Sends 100 Trying → 180 Ringing → 200 OK
- Extracts RTP endpoint from SDP body
- Handles BYE to detect call end

### RTP Audio Pipeline
```
Genesys RTP (G.711 ulaw) → ulaw_to_pcm() → UtteranceBuffer
→ silence detection (800ms threshold)
→ Groq Whisper STT → text
→ Groq LLM agent → response text
→ Piper TTS → PCM audio → pcm_to_ulaw()
→ RTP packets → Genesys
```

### Genesys Integration
- OAuth2 client credentials flow for API auth
- Outbound call trigger via POST /api/v2/conversations/calls
- Genesys places the PSTN call via Verizon carrier
- Audio bridged to our SIP endpoint via corporate SIP trunk

---

## Call Flow

```
1. POST /test/run → load test case JSON
2. Orchestrator → Genesys API → trigger outbound call
3. Genesys → dials US number → bridges audio → SIP INVITE to us
4. SIP server accepts → 200 OK → RTP stream established
5. Loop per turn:
   a. RTP audio arrives from voice AI
   b. UtteranceBuffer detects end of utterance (800ms silence)
   c. Groq Whisper transcribes full utterance
   d. Keyword checkpoints checked inline (e.g. "email" → hit auth endpoint)
   e. Groq LLM agent decides response
   f. Piper TTS synthesizes audio
   g. Audio sent back over RTP
6. Agent signals INTENT_COMPLETE or INTENT_FAILED
7. SIP BYE sent → call ends
8. Post-call: Evaluator runs LLM + rules scoring
9. Logger writes JSON + transcript to disk
```

---

## Test Case Schema

```json
{
  "test_id": "return_flow_001",
  "name": "Order Return Flow",
  "max_turns": 10,
  "persona": {
    "name": "John Smith",
    "order_number": "ORD-789456",
    "item": "Blue Wireless Headphones",
    "quantity": 1,
    "return_reason": "Product is defective, stopped working after 2 days"
  },
  "auth": {
    "endpoint": "http://internal-endpoint/confirm",
    "method": "POST",
    "trigger_phrase": "sent you an email",
    "body": { "order_number": "ORD-789456" }
  },
  "checkpoints": [
    {
      "id": "CP1",
      "name": "Item Verification",
      "description": "Voice AI correctly reads back item and quantity",
      "type": "llm"
    },
    {
      "id": "CP2",
      "name": "Auth Email Triggered",
      "type": "keyword",
      "keywords": ["email", "sent you", "verify", "verification"]
    },
    {
      "id": "CP3",
      "name": "Return Confirmed",
      "description": "Voice AI confirms return before ending call",
      "type": "llm"
    }
  ],
  "success_criteria": "All checkpoints must pass",
  "timeout_seconds": 120
}
```

---

## Output Formats

### JSON Log (`logs/return_flow_001_YYYYMMDD_HHMMSS.json`)
```json
{
  "test_id": "return_flow_001",
  "run_id": "20260405_172217",
  "result": "PASS",
  "score": 8.5,
  "checkpoints": {
    "CP1": { "passed": true, "reason": "Voice AI correctly stated item and quantity" },
    "CP2": { "passed": true, "reason": "Keyword 'email' found" },
    "CP3": { "passed": true, "reason": "Return confirmation detected" }
  },
  "transcript": [
    { "speaker": "VOICE_AI", "text": "Hello, how can I help?" },
    { "speaker": "CUSTOMER", "text": "I want to return an item." }
  ]
}
```

### Plain Text Transcript (`logs/return_flow_001_YYYYMMDD_HHMMSS_transcript.txt`)

---

## Problems Hit and Solutions

### 1. FreeSWITCH packaging completely broken
**Problem:** Every public Docker image dead or paywalled. SignalWire repo requires token.
**Solution:** Wrote custom Python SIP server from scratch. Handles INVITE/ACK/BYE, extracts SDP, fires callbacks. No external SIP server needed.

### 2. SIP library issues
**Problem:** `aioSIP==0.3.0` doesn't exist. `sipsimple` native deps fail on Docker.
**Solution:** Removed all SIP libraries. Pure Python UDP socket implementation.

### 3. Piper TTS download URL broken
**Problem:** Original Piper binary URL 404. New repo uses Python wheel not binary.
**Solution:** Install via pip (`piper-tts==1.4.2`). Piper synthesize writing empty WAV — espeak fallback kicks in.

### 4. Whisper model too slow
**Problem:** Local `base` model takes 200+ seconds per transcription on CPU.
**Solution:** Switched to Groq Whisper API (`whisper-large-v3-turbo`). Fast, free tier.

### 5. STT transcribing per chunk not per utterance
**Problem:** `UtteranceBuffer` not wired correctly. Every 320-byte RTP packet sent to STT directly.
**Solution:** Fixed buffer — accumulates chunks, fires STT only after 800ms silence.

### 6. Docker networking on Windows
**Problem:** `network_mode: host` doesn't work on Windows Docker Desktop.
**Solution:** Use bridge network + port mapping. Use container names for inter-container communication.

### 7. Groq model decommissioned
**Problem:** `llama-3.1-70b-versatile` shut down January 2025.
**Solution:** Updated to `llama-3.3-70b-versatile`.

### 8. Mock timing issue
**Problem:** Mock sends SIP INVITE before orchestrator SIP server is ready.
**Solution:** Moved SIP server startup to `initialize()` at Flask startup — always listening before any test runs.

---

## Current Status (POC)

### Working
- Full SIP handshake (INVITE → 100 → 180 → 200 OK → ACK → BYE)
- Bidirectional RTP audio stream
- Groq Whisper STT transcribing voice AI utterances
- Groq LLM agent driving conversation with persona
- espeak TTS sending customer audio
- Post-call evaluator (rules + LLM scoring)
- JSON logs + transcript files written per run
- Flask API (start test, poll status, fetch logs)
- Mock Genesys for local testing without real infrastructure

### Known Issues / TODO
- Piper TTS synthesize() returns empty WAV — espeak fallback always used
- STT transcribing per 320-byte chunk instead of per full utterance (UtteranceBuffer fix needed)
- Mock conversation doesn't wait for customer audio before progressing
- Groq Whisper rate limit (20 RPM) hit during rapid-fire chunk transcription
- UI not built yet

### Next Steps
1. Fix UtteranceBuffer — accumulate chunks, fire STT once per utterance
2. Fix Piper TTS — WAV always empty, debug synthesize() write path
3. Fix mock — wait for customer audio before sending next turn
4. Build React UI dashboard
5. Get Genesys credentials from admin
6. Test with real SIP endpoint

---

## Environment Variables

```env
# Genesys Auth
GENESYS_CLIENT_ID=
GENESYS_CLIENT_SECRET=
GENESYS_REGION=mypurecloud.com

# Outbound Call
GENESYS_QUEUE_ID=
GENESYS_CALLER_ID=

# SIP + RTP
LOCAL_IP=127.0.0.1
YOUR_SIP_PORT=5060
RTP_LOCAL_PORT=10000

# Target
VOICE_AI_PHONE_NUMBER=

# LLM + STT
GROQ_API_KEY=

# TTS
PIPER_MODEL=/app/models/en_US-lessac-medium.onnx

# Logging
LOG_DIR=/app/logs
TEST_CASES_DIR=/app/test_cases
CALL_CONNECT_TIMEOUT=30
```

---

## Dependencies

```txt
flask==3.0.3
requests==2.31.0
groq==0.9.0
numpy==1.26.4
aiofiles==23.2.1
python-dotenv==1.0.1
docker==7.0.0
piper-tts==1.4.2
httpx==0.27.0
```