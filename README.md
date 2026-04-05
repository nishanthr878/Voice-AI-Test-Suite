# Voice AI Testing Suite

Automated testing tool that replaces manual call testing of voice AI systems. A synthetic AI customer calls your voice AI via SIP, conducts a full conversation, and produces structured pass/fail results with transcripts and scores.

---

## How It Works

```
You define a test case (intent + persona + checkpoints)
        ↓
System triggers Genesys to place an outbound call
        ↓
Genesys dials your voice AI number via Verizon
        ↓
Audio bridged to your SIP endpoint
        ↓
Synthetic customer conducts the conversation
        ↓
Post-call evaluation → PASS/FAIL + score + transcript
```

---

## Project Structure

```
voice-ai-tester/
├── orchestrator/
│   ├── genesys.py          # Genesys OAuth2 + outbound call trigger
│   ├── sip_server.py       # Accepts SIP INVITE from Genesys
│   ├── rtp_handler.py      # Bidirectional RTP audio (G.711)
│   ├── stt.py              # Groq Whisper speech-to-text
│   ├── tts.py              # Piper TTS text-to-speech
│   ├── agent.py            # Groq LLM synthetic customer
│   ├── evaluator.py        # Rules engine + LLM scoring
│   ├── logger.py           # JSON log + transcript writer
│   ├── main.py             # Flask API + orchestration
│   ├── mock_genesys.py     # Mock for local testing
│   ├── Dockerfile
│   └── requirements.txt
├── test_cases/
│   └── return_flow_001.json
├── logs/                   # Test run outputs (gitignored)
├── docker-compose.yml
├── .env                    # API keys (gitignored)
└── .gitignore
```

---

## Prerequisites

- Docker Desktop (Windows/Mac)
- Groq API key — free at [console.groq.com](https://console.groq.com)
- Genesys Cloud credentials (for production use)

---

## Setup

### 1. Clone and configure

```bash
git clone <repo>
cd voice-ai-tester
cp .env.example .env
# Fill in your API keys in .env
```

### 2. Build and start

```bash
docker-compose up --build
```

### 3. Verify it's running

```bash
curl http://localhost:8000/health
```

Expected:
```json
{"status": "ok", "genesys_connected": true, "sip_listening": true}
```

---

## Running Tests

### With mock (no Genesys needed)

Terminal 1 — start the orchestrator:
```bash
docker-compose up --build
```

Terminal 2 — start the mock Genesys:
```bash
docker exec -it orchestrator python mock_genesys.py
```

Terminal 3 — trigger a test:
```bash
curl -X POST http://localhost:8000/test/run \
  -H "Content-Type: application/json" \
  -d '{"test_id": "return_flow_001"}'
```

Poll for results:
```bash
curl http://localhost:8000/test/status
```

### With real Genesys

Make sure `.env` has real Genesys credentials, then trigger a test the same way.

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check + credential verify |
| POST | `/test/run` | Start a test run |
| GET | `/test/status` | Poll current test status |
| GET | `/test/cases` | List available test cases |
| GET | `/logs` | List all log files |
| GET | `/logs/<filename>` | Fetch specific log |

---

## Writing Test Cases

Create a JSON file in `test_cases/`. The filename is the `test_id`.

```json
{
  "test_id": "your_test_id",
  "name": "Human readable name",
  "max_turns": 10,

  "persona": {
    "name": "Customer Name",
    "order_number": "ORD-123456",
    "item": "Product Name",
    "quantity": 1,
    "return_reason": "Reason for return"
  },

  "auth": {
    "endpoint": "http://your-auth-endpoint/confirm",
    "method": "POST",
    "trigger_phrase": "sent you an email",
    "body": { "order_number": "ORD-123456" }
  },

  "checkpoints": [
    {
      "id": "CP1",
      "name": "Item Verification",
      "description": "Voice AI reads back correct item and quantity",
      "type": "llm"
    },
    {
      "id": "CP2",
      "name": "Auth Email Triggered",
      "type": "keyword",
      "keywords": ["email", "sent you", "verify"]
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

### Checkpoint types

**`keyword`** — fast, inline. Fires mid-call when any keyword appears in voice AI speech.

**`llm`** — semantic, post-call. LLM judges whether the checkpoint was satisfied based on full transcript.

---

## Environment Variables

```env
# Genesys (required for production)
GENESYS_CLIENT_ID=
GENESYS_CLIENT_SECRET=
GENESYS_REGION=mypurecloud.com
GENESYS_QUEUE_ID=
GENESYS_CALLER_ID=

# For mock testing
GENESYS_REGION=127.0.0.1:9000

# SIP + RTP
LOCAL_IP=127.0.0.1
YOUR_SIP_PORT=5060
RTP_LOCAL_PORT=10000

# Target number
VOICE_AI_PHONE_NUMBER=+1XXXXXXXXXX

# LLM + STT (required)
GROQ_API_KEY=your_key_here

# TTS
PIPER_MODEL=/app/models/en_US-lessac-medium.onnx

# Storage
LOG_DIR=/app/logs
TEST_CASES_DIR=/app/test_cases
CALL_CONNECT_TIMEOUT=30
```

---

## Output

Each test run produces two files in `logs/`:

**`{test_id}_{run_id}.json`** — structured log with full transcript, checkpoint results, score, and metadata.

**`{test_id}_{run_id}_transcript.txt`** — human readable conversation transcript with checkpoint results and evaluation summary.

---

## Known Issues (POC)

- Piper TTS falls back to espeak — Piper synthesize() writes empty WAV
- STT transcribes per RTP chunk instead of per full utterance — UtteranceBuffer fix pending
- Mock voice AI doesn't wait for customer audio between turns
- Groq Whisper free tier rate limit (20 RPM) hit under rapid transcription
- React UI not yet built — use API directly

---

## Genesys Admin Requirements

To use with real Genesys:

1. OAuth2 client with `conversation:calls:create` permission
2. Outbound Architect flow that dials external number and bridges audio to your SIP endpoint
3. SIP trunk pointing to your machine IP on port 5060
4. RTP ports 10000-10100 UDP open between Genesys and your machine

---

## Tech Stack

| Layer | Tool |
|---|---|
| SIP | Custom Python UDP server |
| RTP | Raw UDP sockets (G.711 ulaw) |
| STT | Groq Whisper API |
| TTS | Piper TTS + espeak fallback |
| LLM | Groq (Llama 3.3 70B) |
| API | Flask |
| Infrastructure | Docker |
