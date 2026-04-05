import os
from groq import Groq
from dotenv import load_dotenv

load_dotenv()


class Evaluator:
    """
    Post-call evaluator.
    Stage 1: Rules engine — keyword matching for fast checkpoints
    Stage 2: LLM judge — semantic evaluation for complex checkpoints
    Stage 3: Overall scoring — intent completion + handling quality
    """

    def __init__(self):
        self.client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        self.model = "llama-3.3-70b-versatile"

    def evaluate(
        self,
        transcript: list,
        persona: dict,
        checkpoints: list
    ) -> dict:
        """
        Run full evaluation on completed call.

        Args:
            transcript: list of {speaker, text, timestamp} dicts
            persona: customer persona from test case
            checkpoints: list of checkpoint definitions

        Returns:
            {
                checkpoints: {CP1: {passed, reason}, ...},
                overall_result: "PASS" or "FAIL",
                score: float (1-10),
                summary: str
            }
        """
        print("[EVALUATOR] Starting post-call evaluation")

        checkpoint_results = {}

        for checkpoint in checkpoints:
            cp_id = checkpoint["id"]
            cp_type = checkpoint["type"]

            print(f"[EVALUATOR] Evaluating checkpoint {cp_id} ({cp_type})")

            if cp_type == "keyword":
                passed, reason = self._evaluate_keyword(
                    checkpoint,
                    transcript
                )
            elif cp_type == "llm":
                passed, reason = self._evaluate_llm(
                    checkpoint,
                    transcript,
                    persona
                )
            else:
                passed, reason = False, f"Unknown checkpoint type: {cp_type}"

            checkpoint_results[cp_id] = {
                "passed": passed,
                "reason": reason,
                "name": checkpoint["name"]
            }

            status = "PASS" if passed else "FAIL"
            print(f"[EVALUATOR] {cp_id}: {status} — {reason}")

        # Overall result — all checkpoints must pass
        all_passed = all(
            cp["passed"]
            for cp in checkpoint_results.values()
        )
        overall_result = "PASS" if all_passed else "FAIL"

        # LLM overall score
        score, summary = self._score_overall(
            transcript,
            persona,
            checkpoint_results
        )

        print(f"[EVALUATOR] Overall: {overall_result} | Score: {score}/10")

        return {
            "checkpoints": checkpoint_results,
            "overall_result": overall_result,
            "score": score,
            "summary": summary
        }

    def _evaluate_keyword(
        self,
        checkpoint: dict,
        transcript: list
    ) -> tuple[bool, str]:
        """
        Check if any keyword appears in voice AI utterances.
        Fast, no LLM call needed.
        """
        keywords = [kw.lower() for kw in checkpoint.get("keywords", [])]

        # Only check voice AI turns — not customer turns
        voice_ai_text = " ".join([
            turn["text"].lower()
            for turn in transcript
            if turn["speaker"] == "VOICE_AI"
        ])

        hit_keywords = [kw for kw in keywords if kw in voice_ai_text]

        if hit_keywords:
            return True, f"Keywords found: {', '.join(hit_keywords)}"

        return False, f"No keywords found. Looking for: {', '.join(keywords)}"

    def _evaluate_llm(
        self,
        checkpoint: dict,
        transcript: list,
        persona: dict
    ) -> tuple[bool, str]:
        """
        Use LLM to evaluate checkpoints requiring semantic understanding.
        E.g. 'did the voice AI correctly identify the item and quantity?'
        """
        transcript_text = self._format_transcript(transcript)

        prompt = f"""You are evaluating a voice AI customer support call.

CHECKPOINT: {checkpoint['name']}
WHAT TO CHECK: {checkpoint['description']}

CUSTOMER PERSONA:
- Order Number: {persona['order_number']}
- Item: {persona['item']}
- Quantity: {persona['quantity']}

TRANSCRIPT:
{transcript_text}

Did the voice AI satisfy this checkpoint?

Reply in exactly this format:
RESULT: YES or NO
REASON: one sentence explaining why"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict QA evaluator. "
                        "Be precise and objective. "
                        "Only reply in the exact format requested."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=150,
            temperature=0.0  # zero temp = deterministic evaluation
        )

        raw = response.choices[0].message.content.strip()

        # Parse response
        passed, reason = self._parse_llm_result(raw)
        return passed, reason

    def _score_overall(
        self,
        transcript: list,
        persona: dict,
        checkpoint_results: dict
    ) -> tuple[float, str]:
        """
        Ask LLM to score the overall call quality 1-10
        and provide a summary of what went well and what failed.
        """
        transcript_text = self._format_transcript(transcript)

        passed_count = sum(
            1 for cp in checkpoint_results.values() if cp["passed"]
        )
        total_count = len(checkpoint_results)

        checkpoint_summary = "\n".join([
            f"- {cp_id} ({data['name']}): "
            f"{'PASS' if data['passed'] else 'FAIL'} — {data['reason']}"
            for cp_id, data in checkpoint_results.items()
        ])

        prompt = f"""You are scoring a voice AI customer support interaction.

CUSTOMER INTENT: Complete a return for order {persona['order_number']}
CHECKPOINTS PASSED: {passed_count}/{total_count}

CHECKPOINT DETAILS:
{checkpoint_summary}

FULL TRANSCRIPT:
{transcript_text}

Score this interaction from 1-10 based on:
- Intent completion (did the return get processed?)
- Accuracy (did the voice AI identify correct item/quantity?)
- Flow quality (was the conversation smooth and natural?)
- Error handling (did the voice AI handle edge cases well?)

Reply in exactly this format:
SCORE: number between 1 and 10
SUMMARY: 2-3 sentences covering what worked and what failed"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict QA evaluator for voice AI systems. "
                        "Be objective and precise."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=200,
            temperature=0.0
        )

        raw = response.choices[0].message.content.strip()

        # Parse score and summary
        score = 5.0  # default
        summary = raw

        for line in raw.split("\n"):
            if line.startswith("SCORE:"):
                try:
                    score = float(line.replace("SCORE:", "").strip())
                except ValueError:
                    pass
            elif line.startswith("SUMMARY:"):
                summary = line.replace("SUMMARY:", "").strip()

        return score, summary

    def _parse_llm_result(self, raw: str) -> tuple[bool, str]:
        """
        Parse LLM checkpoint evaluation response.
        Expected format:
            RESULT: YES or NO
            REASON: explanation
        """
        passed = False
        reason = "Could not parse evaluator response"

        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("RESULT:"):
                result_text = line.replace("RESULT:", "").strip().upper()
                passed = result_text == "YES"
            elif line.startswith("REASON:"):
                reason = line.replace("REASON:", "").strip()

        return passed, reason

    def _format_transcript(self, transcript: list) -> str:
        """Format transcript list into readable text for LLM."""
        lines = []
        for turn in transcript:
            speaker = turn["speaker"]
            text = turn["text"]
            lines.append(f"{speaker}: {text}")
        return "\n".join(lines)