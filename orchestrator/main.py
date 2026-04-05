import os
import threading
import time
import json
import requests
from flask import Flask, jsonify, request
from dotenv import load_dotenv

from genesys import GenesysClient
from sip_server import SIPServer
from rtp_handler import RTPHandler
from stt import STTEngine, UtteranceBuffer
from tts import TTSEngine
from agent import CustomerAgent
from evaluator import Evaluator
from logger import TestLogger

load_dotenv()

app = Flask(__name__)

# Global state
current_test = {
    "running": False,
    "test_id": None,
    "run_id": None,
    "result": None,
    "error": None
}

# Component instances — initialized at startup
genesys = GenesysClient()
stt_engine = None
tts_engine = None
sip_server_instance = None

# Global callbacks — updated per test run
_on_call_connected_cb = None
_on_call_ended_cb = None


def _global_on_call_connected(rtp_ip, rtp_port, call_info):
    """Routes SIP callback to current test run."""
    print(f"[MAIN] *** GLOBAL CALLBACK FIRED *** RTP: {rtp_ip}:{rtp_port}")
    if _on_call_connected_cb:
        _on_call_connected_cb(rtp_ip, rtp_port, call_info)
    else:
        print("[MAIN] *** NO CALLBACK REGISTERED ***")


def _global_on_call_ended(call_id):
    """Routes SIP callback to current test run."""
    if _on_call_ended_cb:
        _on_call_ended_cb(call_id)


def load_test_case(test_id: str) -> dict:
    """Load test case JSON from disk."""
    path = os.path.join(
        os.getenv("TEST_CASES_DIR", "/app/test_cases"),
        f"{test_id}.json"
    )
    if not os.path.exists(path):
        raise FileNotFoundError(f"Test case not found: {path}")
    with open(path) as f:
        return json.load(f)


def run_test(test_case: dict):
    """
    Main test execution function.
    Runs in a background thread.
    """
    global _on_call_connected_cb, _on_call_ended_cb

    test_id = test_case["test_id"]
    logger = TestLogger(test_id)
    agent = CustomerAgent(test_case)
    evaluator = Evaluator()

    rtp_handler = None
    call_connected = threading.Event()
    call_ended = threading.Event()
    rtp_info = {}
    current_utterance = {"text": None, "ready": threading.Event()}

    # ----------------------------------------------------------------
    # Callbacks
    # ----------------------------------------------------------------

    def on_call_connected(rtp_ip: str, rtp_port: int, call_info: dict):
        nonlocal rtp_handler

        print(f"[MAIN] Call connected — RTP: {rtp_ip}:{rtp_port}")
        logger.log_call_connected()

        rtp_info["ip"] = rtp_ip
        rtp_info["port"] = rtp_port

        local_rtp_port = int(os.getenv("RTP_LOCAL_PORT", "10000"))

        # Utterance buffer — accumulates RTP chunks, detects silence
        # fires on_utterance_complete only when full utterance is ready
        utterance_buf = UtteranceBuffer(stt_engine)

        def on_audio_chunk(pcm_bytes: bytes):
            """Called per RTP packet — feeds buffer, fires on complete utterance."""
            result = utterance_buf.add_chunk(pcm_bytes)
            if result and result.strip():
                # Only fires when full utterance transcribed — not per chunk
                print(f"[MAIN] Voice AI said: {result}")
                current_utterance["text"] = result
                current_utterance["ready"].set()


        rtp_handler = RTPHandler(
            local_port=local_rtp_port,
            remote_ip=rtp_ip,
            remote_port=rtp_port,
            on_utterance_complete=on_utterance_complete
        )
        rtp_handler.start()
        call_connected.set()

    def on_call_ended(call_id: str):
        print(f"[MAIN] Call ended: {call_id}")
        logger.log_call_end("BYE received")
        call_ended.set()

    def on_utterance_complete(pcm_bytes: bytes):
        text = stt_engine.transcribe(pcm_bytes)
        if text:
            print(f"[MAIN] Voice AI said: {text}")
            current_utterance["text"] = text
            current_utterance["ready"].set()

    # Register callbacks with global SIP server
    _on_call_connected_cb = on_call_connected
    _on_call_ended_cb = on_call_ended

    # DIAGNOSTIC
    print(f"[MAIN] Test started — SIP server running: {sip_server_instance.running}")
    print(f"[MAIN] Waiting for call on port {os.getenv('YOUR_SIP_PORT', '5060')}")
    print(f"[MAIN] Triggering Genesys call now...")

    try:
        # ----------------------------------------------------------------
        # Step 1: Trigger Genesys outbound call
        # ----------------------------------------------------------------

        phone_number = os.getenv("VOICE_AI_PHONE_NUMBER")
        print(f"[MAIN] Triggering outbound call to {phone_number}")

        call_result = genesys.trigger_outbound_call(phone_number)
        conversation_id = call_result.get("conversation_id")
        logger.log_call_start(phone_number, conversation_id)

        # ----------------------------------------------------------------
        # Step 2: Wait for SIP INVITE
        # ----------------------------------------------------------------

        print("[MAIN] Waiting for SIP INVITE from Genesys...")
        timeout = int(os.getenv("CALL_CONNECT_TIMEOUT", "30"))

        if not call_connected.wait(timeout=timeout):
            raise TimeoutError(
                f"No SIP INVITE received within {timeout}s"
            )

        print("[MAIN] Call connected — starting conversation")

        # ----------------------------------------------------------------
        # Step 3: Comfort noise while waiting for greeting
        # ----------------------------------------------------------------

        rtp_handler.send_silence(2000)

        # ----------------------------------------------------------------
        # Step 4: Conversation loop
        # ----------------------------------------------------------------

        max_turns = test_case.get("max_turns", 15)
        turn = 0
        intent_complete = False
        intent_failed = False

        while (
            turn < max_turns
            and not intent_complete
            and not intent_failed
        ):
            print(f"[MAIN] Turn {turn + 1} — waiting for voice AI...")
            current_utterance["ready"].clear()
            current_utterance["text"] = None

            if not current_utterance["ready"].wait(timeout=15):
                print("[MAIN] Timeout waiting for voice AI")
                logger.log_event("TIMEOUT", "No audio from voice AI")
                intent_failed = True
                break

            voice_ai_text = current_utterance["text"]
            logger.log_turn("VOICE_AI", voice_ai_text)

            # Keyword checkpoint check
            auth_triggered = False
            for checkpoint in test_case.get("checkpoints", []):
                if checkpoint["type"] == "keyword":
                    keywords = [
                        kw.lower()
                        for kw in checkpoint.get("keywords", [])
                    ]
                    if any(
                        kw in voice_ai_text.lower()
                        for kw in keywords
                    ):
                        print(
                            f"[MAIN] Checkpoint {checkpoint['id']} hit"
                        )
                        logger.log_event(
                            "CHECKPOINT_HIT",
                            f"{checkpoint['id']}: keyword found"
                        )
                        auth_config = test_case.get("auth", {})
                        if auth_config.get("endpoint"):
                            auth_triggered = _hit_auth_endpoint(
                                auth_config, logger
                            )

            # Send silence while agent thinks
            silence_thread = threading.Thread(
                target=rtp_handler.send_silence,
                args=(3000,),
                daemon=True
            )
            silence_thread.start()

            response_text, intent_complete, intent_failed = (
                agent.get_response(voice_ai_text, auth_triggered)
            )

            silence_thread.join()

            logger.log_turn("CUSTOMER", response_text)
            print(f"[MAIN] Customer says: {response_text}")

            audio = tts_engine.synthesize(response_text)
            rtp_handler.send_audio(audio)

            turn += 1

        # ----------------------------------------------------------------
        # Step 5: End call
        # ----------------------------------------------------------------

        end_reason = (
            "INTENT_COMPLETE" if intent_complete
            else "INTENT_FAILED" if intent_failed
            else "MAX_TURNS_REACHED"
        )

        logger.log_call_end(end_reason)
        logger.log_event("CALL_END_REASON", end_reason)

        sip_server_instance.send_bye()

        if rtp_handler:
            rtp_handler.stop()

        # ----------------------------------------------------------------
        # Step 6: Evaluate
        # ----------------------------------------------------------------

        print("[MAIN] Running post-call evaluation...")

        eval_result = evaluator.evaluate(
            transcript=logger.get_transcript_for_evaluator(),
            persona=test_case["persona"],
            checkpoints=test_case["checkpoints"]
        )

        # ----------------------------------------------------------------
        # Step 7: Finalize
        # ----------------------------------------------------------------

        logger.finalize(
            result=eval_result["overall_result"],
            score=eval_result["score"],
            summary=eval_result["summary"],
            checkpoint_results=eval_result["checkpoints"]
        )

        current_test["result"] = logger.get_summary()
        current_test["running"] = False

    except Exception as e:
        print(f"[MAIN] Test failed: {e}")
        logger.log_event("ERROR", str(e))
        logger.log_call_end("ERROR")
        current_test["error"] = str(e)
        current_test["running"] = False

        if rtp_handler:
            rtp_handler.stop()

    finally:
        # Clear callbacks — ready for next test
        _on_call_connected_cb = None
        _on_call_ended_cb = None


def _hit_auth_endpoint(auth_config: dict, logger: TestLogger) -> bool:
    endpoint = auth_config.get("endpoint")
    method = auth_config.get("method", "POST").upper()
    body = auth_config.get("body", {})
    headers = auth_config.get("headers", {})

    print(f"[MAIN] Hitting auth endpoint: {endpoint}")

    try:
        if method == "POST":
            response = requests.post(
                endpoint, json=body, headers=headers, timeout=5
            )
        else:
            response = requests.get(
                endpoint, headers=headers, timeout=5
            )

        success = response.status_code in (200, 201, 204)
        logger.log_event(
            "AUTH_TRIGGERED",
            f"Endpoint: {endpoint} Status: {response.status_code}"
        )
        return success

    except Exception as e:
        print(f"[MAIN] Auth endpoint error: {e}")
        logger.log_event("AUTH_FAILED", str(e))
        return False


# ----------------------------------------------------------------
# Flask routes
# ----------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    genesys_ok = genesys.verify_credentials()
    return jsonify({
        "status": "ok",
        "genesys_connected": genesys_ok,
        "sip_listening": sip_server_instance is not None
    })


@app.route("/test/run", methods=["POST"])
def run_test_route():
    if current_test["running"]:
        return jsonify({"error": "Test already running"}), 400

    data = request.json
    test_id = data.get("test_id")

    if not test_id:
        return jsonify({"error": "test_id required"}), 400

    try:
        test_case = load_test_case(test_id)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404

    current_test["running"] = True
    current_test["test_id"] = test_id
    current_test["result"] = None
    current_test["error"] = None

    thread = threading.Thread(
        target=run_test,
        args=(test_case,),
        daemon=True
    )
    thread.start()

    return jsonify({
        "status": "test_started",
        "test_id": test_id
    })


@app.route("/test/status", methods=["GET"])
def test_status():
    return jsonify({
        "running": current_test["running"],
        "test_id": current_test["test_id"],
        "result": current_test["result"],
        "error": current_test["error"]
    })


@app.route("/test/cases", methods=["GET"])
def list_test_cases():
    test_dir = os.getenv("TEST_CASES_DIR", "/app/test_cases")
    if not os.path.exists(test_dir):
        return jsonify({"test_cases": []})
    cases = [
        f.replace(".json", "")
        for f in os.listdir(test_dir)
        if f.endswith(".json")
    ]
    return jsonify({"test_cases": cases})


@app.route("/logs", methods=["GET"])
def list_logs():
    log_dir = os.getenv("LOG_DIR", "/app/logs")
    if not os.path.exists(log_dir):
        return jsonify({"logs": []})
    logs = sorted([
        f for f in os.listdir(log_dir)
        if f.endswith(".json")
    ], reverse=True)
    return jsonify({"logs": logs})


@app.route("/logs/<filename>", methods=["GET"])
def get_log(filename: str):
    log_dir = os.getenv("LOG_DIR", "/app/logs")
    path = os.path.join(log_dir, filename)
    if not os.path.exists(path):
        return jsonify({"error": "Log not found"}), 404
    with open(path) as f:
        return jsonify(json.load(f))

@app.route("/test/audio", methods=["GET"])
def test_audio():
    from rtp_handler import RTPPacket

    test_text = "Hello, I would like to return my order please."
    pcm_bytes = tts_engine.synthesize(test_text)

    # Check silence on raw
    silent_raw = bool(stt_engine.is_silent(pcm_bytes[:320]))

    # Ulaw round-trip
    ulaw_bytes = RTPPacket.pcm_to_ulaw(pcm_bytes)
    decoded_pcm = RTPPacket.ulaw_to_pcm(ulaw_bytes)
    silent_ulaw = bool(stt_engine.is_silent(decoded_pcm[:320]))

    # Transcribe both
    text_raw = stt_engine.transcribe(pcm_bytes)
    text_ulaw = stt_engine.transcribe(decoded_pcm)

    return jsonify({
        "tts_bytes": len(pcm_bytes),
        "silent_raw": silent_raw,
        "silent_ulaw": silent_ulaw,
        "transcription_raw": text_raw,
        "transcription_ulaw": text_ulaw
    })


# ----------------------------------------------------------------
# Startup
# ----------------------------------------------------------------

def initialize():
    global stt_engine, tts_engine, sip_server_instance

    print("[MAIN] Initializing components...")
    

    # STT — load Whisper model once
    stt_engine = STTEngine()
   

    # TTS — verify Piper/espeak
    tts_engine = TTSEngine()

    # SIP server — starts immediately and stays running
    # Ready before any test is triggered
    local_ip = os.getenv("LOCAL_IP", "127.0.0.1")
    local_sip_port = int(os.getenv("YOUR_SIP_PORT", "5060"))

    sip_server_instance = SIPServer(
        local_ip=local_ip,
        local_port=local_sip_port,
        on_call_connected=_global_on_call_connected,
        on_call_ended=_global_on_call_ended
    )
    sip_server_instance.start()

    print(f"[MAIN] SIP server listening on {local_ip}:{local_sip_port}")
    print("[MAIN] All components ready")


if __name__ == "__main__":
    initialize()
    print("[MAIN] Starting Flask on port 8000")
    app.run(host="0.0.0.0", port=8000, debug=False)