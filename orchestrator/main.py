import os
import threading
import time
import json
import requests
from flask import Flask, jsonify, request
from dotenv import load_dotenv

from sip_client import SIPClient
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
stt_engine = None
tts_engine = None


def load_test_case(test_id: str) -> dict:
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
    Runs in background thread.
    Dials vendor SIP URI directly — no Genesys API needed.
    """
    test_id = test_case["test_id"]
    logger = TestLogger(test_id)
    agent = CustomerAgent(test_case)
    evaluator = Evaluator()

    rtp_handler = None
    sip_client = None
    current_utterance = {"text": None, "ready": threading.Event()}

    try:
        # ----------------------------------------------------------------
        # Step 1: Dial vendor SIP URI directly
        # ----------------------------------------------------------------

        target_sip_uri = os.getenv("TARGET_SIP_URI")
        local_ip = os.getenv("LOCAL_IP", "0.0.0.0")
        local_sip_port = int(os.getenv("YOUR_SIP_PORT", "5062"))

        if not target_sip_uri:
            raise Exception("TARGET_SIP_URI not set in .env")

        print(f"[MAIN] Dialing {target_sip_uri} directly")
        logger.log_call_start(target_sip_uri, None)

        sip_client = SIPClient(local_ip, local_sip_port)
        rtp_ip, rtp_port = sip_client.dial(target_sip_uri)

        if not rtp_ip:
            raise Exception(
                f"SIP call failed — no answer from {target_sip_uri}"
            )

        # ----------------------------------------------------------------
        # Step 2: Start RTP handler immediately
        # No waiting — we already connected
        # ----------------------------------------------------------------

        local_rtp_port = int(os.getenv("RTP_LOCAL_PORT", "10000"))
        utterance_buf = UtteranceBuffer(stt_engine)

        def on_audio_chunk(pcm_bytes: bytes):
            """Per RTP packet — feeds buffer, fires on complete utterance."""
            result = utterance_buf.add_chunk(pcm_bytes)
            if result and result.strip():
                print(f"[MAIN] Voice AI said: {result}")
                current_utterance["text"] = result
                current_utterance["ready"].set()

        rtp_handler = RTPHandler(
            local_port=local_rtp_port,
            remote_ip=rtp_ip,
            remote_port=rtp_port,
            on_utterance_complete=on_audio_chunk
        )
        rtp_handler.start()
        logger.log_call_connected()

        print(f"[MAIN] Call connected — RTP: {rtp_ip}:{rtp_port}")
        print("[MAIN] Starting conversation")

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
                        print(f"[MAIN] Checkpoint {checkpoint['id']} hit")
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

        # Hang up
        if sip_client:
            sip_client.hangup()

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

        if sip_client:
            sip_client.hangup()
        if rtp_handler:
            rtp_handler.stop()


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
    target = os.getenv("TARGET_SIP_URI", "not configured")
    return jsonify({
        "status": "ok",
        "target_sip_uri": target,
        "local_ip": os.getenv("LOCAL_IP"),
        "sip_port": os.getenv("YOUR_SIP_PORT", "5062")
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