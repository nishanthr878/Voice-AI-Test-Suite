import os
import json
from datetime import datetime
from typing import Optional


class TestLogger:
    """
    Handles all logging for a single test run.
    Writes:
    - Structured JSON log (machine readable)
    - Plain text transcript (human readable)
    """

    def __init__(self, test_id: str):
        self.test_id = test_id
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_dir = os.getenv("LOG_DIR", "/app/logs")
        os.makedirs(self.log_dir, exist_ok=True)

        # Data accumulated during the call
        self.transcript = []
        self.events = []
        self.checkpoints = {}
        self.start_time = None
        self.end_time = None
        self.result = None
        self.score = None
        self.summary = None

        # Genesys metadata
        self.conversation_id = None
        self.phone_number = None

        print(f"[LOGGER] Initialized run: {self.run_id}")

    # ------------------------------------------------------------------
    # Call lifecycle
    # ------------------------------------------------------------------

    def log_call_start(
        self,
        phone_number: str,
        conversation_id: Optional[str] = None
    ):
        """Call this the moment Genesys triggers the outbound call."""
        self.start_time = datetime.now().isoformat()
        self.phone_number = phone_number
        self.conversation_id = conversation_id
        self.log_event(
            "CALL_START",
            f"Outbound call triggered to {phone_number}"
        )

    def log_call_connected(self):
        """Call this when SIP INVITE is received and accepted."""
        self.log_event("CALL_CONNECTED", "SIP handshake complete, RTP active")

    def log_call_end(self, reason: str):
        """Call this when the call ends for any reason."""
        self.end_time = datetime.now().isoformat()
        self.log_event("CALL_END", reason)

    # ------------------------------------------------------------------
    # Conversation turns
    # ------------------------------------------------------------------

    def log_turn(self, speaker: str, text: str):
        """
        Log a single conversation turn.
        speaker: "VOICE_AI" or "CUSTOMER"
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "speaker": speaker,
            "text": text
        }
        self.transcript.append(entry)
        print(f"[TRANSCRIPT] {speaker}: {text}")

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def log_event(self, event_type: str, detail: str):
        """
        Log a system event.
        Examples: AUTH_TRIGGERED, CHECKPOINT_HIT, TIMEOUT
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event": event_type,
            "detail": detail
        }
        self.events.append(entry)
        print(f"[EVENT] {event_type}: {detail}")

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    def log_checkpoint(
        self,
        checkpoint_id: str,
        name: str,
        passed: bool,
        reason: str
    ):
        """Log a checkpoint evaluation result."""
        self.checkpoints[checkpoint_id] = {
            "name": name,
            "passed": passed,
            "reason": reason,
            "timestamp": datetime.now().isoformat()
        }
        status = "PASS" if passed else "FAIL"
        print(f"[CHECKPOINT] {checkpoint_id} {status}: {reason}")

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def finalize(
        self,
        result: str,
        score: float,
        summary: str,
        checkpoint_results: dict
    ):
        """
        Finalize the test run.
        Call this after evaluation is complete.
        Writes JSON log and transcript to disk.
        """
        self.result = result
        self.score = score
        self.summary = summary

        # Merge evaluator checkpoint results into logger
        for cp_id, cp_data in checkpoint_results.items():
            self.checkpoints[cp_id] = {
                **cp_data,
                "timestamp": datetime.now().isoformat()
            }

        self._write_json()
        self._write_transcript()

        print(f"\n{'='*50}")
        print(f"TEST RESULT : {result}")
        print(f"SCORE       : {score}/10")
        print(f"SUMMARY     : {summary}")
        print(f"{'='*50}\n")

    def _write_json(self):
        """Write structured JSON log."""
        duration_seconds = None
        if self.start_time and self.end_time:
            start = datetime.fromisoformat(self.start_time)
            end = datetime.fromisoformat(self.end_time)
            duration_seconds = round((end - start).total_seconds(), 2)

        log = {
            "test_id": self.test_id,
            "run_id": self.run_id,
            "phone_number": self.phone_number,
            "conversation_id": self.conversation_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_seconds": duration_seconds,
            "result": self.result,
            "score": self.score,
            "summary": self.summary,
            "checkpoints": self.checkpoints,
            "events": self.events,
            "transcript": self.transcript
        }

        filename = f"{self.test_id}_{self.run_id}.json"
        path = os.path.join(self.log_dir, filename)

        with open(path, "w") as f:
            json.dump(log, f, indent=2)

        print(f"[LOGGER] JSON log written: {path}")
        return path

    def _write_transcript(self):
        """Write human readable transcript."""
        filename = f"{self.test_id}_{self.run_id}_transcript.txt"
        path = os.path.join(self.log_dir, filename)

        with open(path, "w") as f:
            # Header
            f.write(f"TEST RUN : {self.test_id} | {self.run_id}\n")
            f.write(f"RESULT   : {self.result}\n")
            f.write(f"SCORE    : {self.score}/10\n")
            f.write(f"PHONE    : {self.phone_number}\n")
            f.write(f"START    : {self.start_time}\n")
            f.write(f"END      : {self.end_time}\n")
            f.write("=" * 60 + "\n\n")

            # Transcript
            f.write("CONVERSATION\n")
            f.write("-" * 60 + "\n")
            for turn in self.transcript:
                f.write(f"[{turn['timestamp']}]\n")
                f.write(f"{turn['speaker']}: {turn['text']}\n\n")

            # Checkpoints
            f.write("-" * 60 + "\n")
            f.write("CHECKPOINTS\n")
            f.write("-" * 60 + "\n")
            for cp_id, cp_data in self.checkpoints.items():
                status = "PASS" if cp_data["passed"] else "FAIL"
                f.write(f"{cp_id} [{status}] {cp_data['name']}\n")
                f.write(f"  {cp_data['reason']}\n\n")

            # Events
            f.write("-" * 60 + "\n")
            f.write("EVENTS\n")
            f.write("-" * 60 + "\n")
            for event in self.events:
                f.write(
                    f"[{event['timestamp']}] "
                    f"{event['event']}: {event['detail']}\n"
                )

            # Summary
            f.write("\n" + "-" * 60 + "\n")
            f.write("EVALUATION SUMMARY\n")
            f.write("-" * 60 + "\n")
            f.write(f"{self.summary}\n")

        print(f"[LOGGER] Transcript written: {path}")
        return path

    def get_transcript_for_evaluator(self) -> list:
        """Return transcript in format expected by evaluator."""
        return self.transcript

    def get_run_id(self) -> str:
        return self.run_id

    def get_summary(self) -> dict:
        """Return lightweight summary for API response."""
        return {
            "test_id": self.test_id,
            "run_id": self.run_id,
            "result": self.result,
            "score": self.score,
            "summary": self.summary,
            "checkpoints": {
                cp_id: {
                    "passed": cp_data["passed"],
                    "name": cp_data["name"]
                }
                for cp_id, cp_data in self.checkpoints.items()
            },
            "duration_seconds": (
                round(
                    (
                        datetime.fromisoformat(self.end_time) -
                        datetime.fromisoformat(self.start_time)
                    ).total_seconds(), 2
                )
                if self.start_time and self.end_time else None
            )
        }