# Voice AI Testing Suite
## Design & Implementation Document

| | |
|---|---|
| **Version** | 1.0 — POC |
| **Status** | Draft |
| **Platform** | macOS (Apple Silicon / Intel) |
| **Scope** | POC → Production Roadmap |

---

## 1. Overview

This document describes the design and implementation plan for an automated Voice AI Testing Suite. The system replaces manual call-based testing by deploying a synthetic AI customer that calls a target Voice AI via SIP, executes a predefined test scenario, and produces structured pass/fail results with full transcripts.

The primary use case is regression and functional testing of Genesys-hosted voice bots without requiring human testers to make calls manually.

### 1.1 Problem Statement

Current state: A QA engineer manually dials the company voice AI, navigates the conversation flow, and records observations by hand. This process is:

- **Slow** — one test run takes 5–10 minutes of human time
- **Non-repeatable** — human variation affects results
- **Unscalable** — cannot run parallel test scenarios
- **Undocumented** — no structured logs or transcripts

### 1.2 Solution

An automated pipeline that:

1. Accepts a JSON test case defining intent, customer persona, and success checkpoints
2. Originates a SIP call to the target Voice AI endpoint
3. Conducts the conversation using an LLM-powered synthetic customer
4. Transcribes both sides of the conversation in real-time using STT
5. Triggers mid-call actions (e.g. auth endpoint) automatically
6. Evaluates the result post-call using a rules engine and LLM judge
7. Outputs structured logs, transcript, and pass/fail verdict to a web UI

---

## 2. System Architecture

### 2.1 High-Level Architecture

```
+--------------------------------------------------+
|             TEST RUNNER (FastAPI)                |
|  Input: test case JSON                           |
|  Output: result, transcript, score               |
+----------+--------------------+------------------+
           |                    |
+----------v------+  +----------v-----------------+
|  FreeSWITCH     |  |     MEDIA PIPELINE         |
|  mod_sofia      |  |  STT: faster-whisper       |
|  SIP INVITE --> |  |  TTS: Piper TTS            |
|  sip:bot@co.com |  |  Audio: sounddevice/socket |
+---------+-------+  +----------+-----------------+
          |      RTP audio (bidirectional)  |
          +---------------------------------+
                           |
+---------------------------v--------------------------+
|          CUSTOMER AGENT (Groq / Llama 3.1 70B)      |
|  Persona + Intent + Conversation history             |
|  Drives turns, detects completion / failure          |
+---------------------------+--------------------------+
                            |  full transcript
+---------------------------v--------------------------+
|                    EVALUATOR                         |
|  Rules engine: keyword checkpoint matching           |
|  LLM judge: intent completion scoring                |
+---------------------------+--------------------------+
                            |
+---------------------------v--------------------------+
|              STORAGE + REACT UI                      |
|  JSON log, txt transcript, pass/fail, score          |
+------------------------------------------------------+
```

### 2.2 Component Responsibilities

| Component | Responsibility |
|---|---|
| **Test Runner** | Orchestrates the entire call lifecycle. Reads test case, triggers FreeSWITCH, manages conversation turns, invokes evaluator, writes logs. |
| **FreeSWITCH** | Handles SIP signaling. Originates a SIP INVITE to the target URI and maintains the RTP audio stream for the call duration. |
| **Media Pipeline** | Bridges RTP audio to/from the Python process. STT converts incoming audio to text; TTS converts customer agent text to audio sent back over RTP. |
| **Customer Agent** | LLM (Llama 3.1 70B via Groq) prompted with persona, intent, and conversation history. Decides what the synthetic customer says each turn. |
| **Evaluator** | Post-call module. Runs keyword rules against transcript, then asks LLM to score intent completion and handling quality. |
| **Storage + UI** | Writes JSON log and plain-text transcript per run. React dashboard displays results, scores, and per-checkpoint status. |

---

## 3. Technology Stack

### 3.1 Component-to-Tool Mapping

| Component | Tool | License / Cost | Purpose |
|---|---|---|---|
| SIP / Call Control | FreeSWITCH + mod_sofia | Open Source (MPL) | Originates SIP call to target URI |
| ESL (call control API) | python-ESL | Open Source | Python controls FreeSWITCH programmatically |
| Speech-to-Text | faster-whisper | Open Source (MIT) | Transcribes voice AI audio in real-time |
| Text-to-Speech | Piper TTS | Open Source (MIT) | Converts customer agent text to audio |
| Customer Agent LLM | Groq API (Llama 3.1 70B) | Free tier available | Drives synthetic customer conversation |
| Evaluator LLM | Groq API (Llama 3.1 70B) | Free tier available | Scores intent completion post-call |
| Audio I/O | sounddevice / RTP socket | Open Source | Pipes audio in/out of FreeSWITCH |
| Orchestrator | Python 3.11 + FastAPI | Open Source | Manages call flow and turn logic |
| Storage | JSON files + SQLite | Open Source | Persists logs and results |
| UI | React + Vite | Open Source | Test runner and results dashboard |

### 3.2 Why These Tools

**FreeSWITCH over Asterisk**
FreeSWITCH's Event Socket Library (ESL) gives Python full programmatic control over call state, audio routing, and events. Asterisk's AGI is older and less suited to real-time bidirectional audio control from Python.

**Groq over Ollama**
Groq runs Llama 3.1 70B inference on dedicated hardware at ~500 tokens/second — significantly faster than local Ollama with consumer GPUs. No GPU required on your Mac. Free tier is sufficient for POC volume.

**faster-whisper over Deepgram**
Fully local, no API key, no cost. Quality matches Deepgram for clear audio. The medium model runs on Mac CPU acceptably; the large-v3 model is better on Apple Silicon MPS.

**Piper TTS over ElevenLabs**
Piper runs locally with zero latency overhead from network calls. Voice quality is robotic but perfectly intelligible to a voice AI — the target listener is a machine, not a human.

---

## 4. Test Case Design

### 4.1 Test Case JSON Schema

Each test case is a single JSON file. This is the contract between the QA engineer and the system.

```json
{
  "test_id": "return_flow_001",
  "name": "Order Return Flow",
  "description": "Tests end-to-end return initiation including auth",

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
      "description": "Voice AI reads back correct item and quantity",
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

### 4.2 Checkpoint Types

| ID | Name | Description | Type | Pass Criteria |
|---|---|---|---|---|
| CP1 | Item Verification | Voice AI correctly reads back item name and quantity after order number is given | LLM judge | Item + quantity match persona |
| CP2 | Auth Email Triggered | Voice AI says it has sent a verification email to the customer | Keyword match | Any keyword hit from list |
| CP3 | Return Confirmed | Voice AI explicitly confirms the return is initiated before ending the call | LLM judge | Confirmation language detected |

### 4.3 Conversation Flow

```
VOICE AI:     Hello, how can I help you today?
CUSTOMER:     Hi, I want to return an item I ordered.

VOICE AI:     Sure, can I get your order number?
CUSTOMER:     Yes, it's ORD-789456.

VOICE AI:     I can see order ORD-789456 — Blue Wireless Headphones,
              quantity 1. What is the reason for return?
              [CHECKPOINT 1 — item verification fires here]

CUSTOMER:     The product is defective, it stopped working after 2 days.

VOICE AI:     I've sent a verification email to your address.
              Please click the link to confirm.
              [CHECKPOINT 2 — keyword 'email' fires here]
              [SYSTEM — hits auth endpoint automatically]

CUSTOMER:     Okay, I've verified it.

VOICE AI:     Your return for ORD-789456 has been initiated.
              [CHECKPOINT 3 — return confirmation fires here]

CUSTOMER:     Thank you, goodbye.
              [INTENT_COMPLETE signal sent to orchestrator]
```

---

## 5. Project Structure

```
voice-ai-tester/
├── freeswitch/
│   ├── sip_profile.xml       # mod_sofia SIP profile config
│   └── dialplan.xml          # Outbound call routing
│
├── orchestrator/
│   ├── main.py               # FastAPI app — call lifecycle + API routes
│   ├── agent.py              # Customer agent (Groq/Llama)
│   ├── pipeline.py           # STT (faster-whisper) + TTS (Piper)
│   ├── evaluator.py          # Rules engine + LLM judge
│   ├── sip_controller.py     # FreeSWITCH ESL bridge
│   └── logger.py             # JSON + transcript logging
│
├── test_cases/
│   └── return_flow.json      # Your return flow test case
│
├── logs/                     # Output: JSON logs + transcripts
│
├── ui/
│   ├── src/
│   │   ├── App.jsx           # Main dashboard
│   │   ├── TestRunner.jsx    # Run test, show live status
│   │   └── ResultView.jsx    # Transcript + checkpoint results
│   └── package.json
│
├── requirements.txt          # Python dependencies
└── README.md                 # Setup instructions
```

---

## 6. Setup Guide (macOS)

### 6.1 Prerequisites

- macOS 12+ (Intel or Apple Silicon)
- Python 3.11+ — install via `pyenv` or Homebrew
- Node.js 18+ — for React UI
- Homebrew — package manager
- Groq API key — free at [console.groq.com](https://console.groq.com)

### 6.2 FreeSWITCH Installation

Docker is the recommended path for POC. It avoids macOS-specific build issues and gives you a clean FreeSWITCH environment with volume-mounted config.

```bash
# Recommended: Docker
docker pull signalwire/freeswitch:latest
docker run -d --name freeswitch \
  -p 5060:5060/udp \
  -p 5060:5060/tcp \
  -p 16384-16484:16384-16484/udp \
  -v $(pwd)/freeswitch/conf:/etc/freeswitch \
  signalwire/freeswitch:latest

# Alternative: Homebrew tap
brew tap signalwire/freeswitch
brew install freeswitch
```

### 6.3 Python Environment

```bash
cd orchestrator
python -m venv venv
source venv/bin/activate

pip install fastapi uvicorn python-ESL groq \
            faster-whisper sounddevice numpy \
            requests aiofiles python-dotenv
```

### 6.4 Piper TTS Setup

```bash
# Download Piper binary for macOS
wget https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_macos_amd64.tar.gz
tar -xzf piper_macos_amd64.tar.gz

# Download voice model (en_US-lessac-medium recommended)
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json

# Test it
echo "Hello, I would like to return my order." | ./piper \
  --model en_US-lessac-medium.onnx --output_file test.wav
```

### 6.5 Environment Variables

```bash
# .env file in project root
GROQ_API_KEY=your_groq_api_key_here
FREESWITCH_HOST=127.0.0.1
FREESWITCH_ESL_PORT=8021
FREESWITCH_ESL_PASSWORD=ClueCon
PIPER_BINARY=./piper/piper
PIPER_MODEL=./models/en_US-lessac-medium.onnx
WHISPER_MODEL=medium          # or large-v3 on Apple Silicon
LOG_DIR=./logs
```

### 6.6 React UI Setup

```bash
cd ui
npm install
npm run dev
# UI available at http://localhost:5173
```

---

## 7. Build Plan

### 7.1 POC Phases

| Phase | Duration | Tasks | Deliverable |
|---|---|---|---|
| Phase 1 | 2–3 hrs | FreeSWITCH Docker setup, SIP profile config, ESL Python connection, originate test call to SIP URI | SIP call established to target |
| Phase 2 | 2–3 hrs | RTP audio bridge, faster-whisper STT integration, Piper TTS integration, bidirectional audio pipeline | Audio flows both directions |
| Phase 3 | 2 hrs | Customer agent prompting, Groq/Llama integration, turn management, intent completion detection | Agent drives full conversation |
| Phase 4 | 1 hr | Rules engine for keyword checkpoints, LLM judge for semantic checkpoints, overall scoring | Pass/fail verdict generated |
| Phase 5 | 1 hr | JSON log writer, transcript writer, FastAPI routes, React UI with results view | Full results visible in UI |

### 7.2 Known Risks

- **FreeSWITCH RTP-to-Python audio bridging is the hardest integration point.** Plan for debugging time here.
- faster-whisper on Mac CPU may have latency on the large model — use medium model for POC.
- Groq free tier has rate limits (~30 req/min). Sufficient for sequential testing, not parallel.
- SIP URI format and network routing must be confirmed with Genesys admin before Phase 1.

---

## 8. Output Formats

### 8.1 JSON Log

```json
{
  "test_id": "return_flow_001",
  "run_id": "20240415_143022",
  "start_time": "2024-04-15T14:30:22Z",
  "end_time": "2024-04-15T14:32:47Z",
  "result": "PASS",
  "score": 8.5,
  "checkpoints": {
    "CP1": { "passed": true, "reason": "Voice AI correctly stated item and quantity" },
    "CP2": { "passed": true, "reason": "Keyword 'email' found in voice AI utterance" },
    "CP3": { "passed": true, "reason": "Return confirmation detected before call end" }
  },
  "events": [
    { "event": "AUTH_TRIGGERED", "detail": "Hit endpoint, status 200" }
  ],
  "transcript": [
    { "speaker": "VOICE_AI", "text": "Hello, how can I help?" },
    { "speaker": "CUSTOMER", "text": "I want to return an item." }
  ]
}
```

### 8.2 Plain Text Transcript

```
TEST RUN: return_flow_001 | 20240415_143022
Result: PASS | Score: 8.5/10
============================================================

CONVERSATION TRANSCRIPT
------------------------------------------------------------
[2024-04-15T14:30:24Z]
VOICE_AI: Hello, thank you for calling. How can I help you today?

[2024-04-15T14:30:27Z]
CUSTOMER: Hi, I want to return an item I ordered.

... (continues)

CHECKPOINT RESULTS
------------------------------------------------------------
CP1: PASS  — Voice AI correctly stated item and quantity
CP2: PASS  — Keyword 'email' found
CP3: PASS  — Return confirmation detected
```

---

## 9. Production Roadmap

### 9.1 POC vs Production

| Concern | POC | Production |
|---|---|---|
| Test execution | Sequential (1 at a time) | Parallel workers (N concurrent calls) |
| LLM | Groq free tier | Groq paid or self-hosted vLLM |
| STT | faster-whisper (local) | faster-whisper cluster or Deepgram |
| SIP | FreeSWITCH Docker (local) | FreeSWITCH cluster + load balancer |
| Storage | JSON files + SQLite | PostgreSQL + S3 for audio files |
| UI | Local React dev server | Deployed web app with auth |
| Auth step | HTTP endpoint call | Pluggable auth adapter per test case |
| Observability | Console logs | Structured logging + Grafana dashboards |

### 9.2 Scaling Considerations

- **Parallel calls** require multiple FreeSWITCH instances or a media server cluster. Each concurrent call needs its own audio pipeline process.
- **Groq rate limits** cap the free tier at ~30 req/min. At 3–5 LLM calls per test run, this supports ~6–10 parallel tests. The paid tier removes this constraint.
- **Audio storage** (WAV files per call) grows quickly. Production needs S3 or equivalent with lifecycle policies.
- **The evaluator** is the easiest component to scale — it is stateless and post-call, so it can run as a separate worker pool.

---

## 10. Dependencies

```
# requirements.txt
fastapi==0.111.0
uvicorn==0.29.0
python-ESL==1.0.0
groq==0.9.0
faster-whisper==1.0.3
sounddevice==0.4.6
numpy==1.26.4
requests==2.31.0
aiofiles==23.2.1
python-dotenv==1.0.1
```

Piper TTS is a standalone binary — not a Python package. Download the macOS binary and voice model separately as described in Section 6.4.