import os
from groq import Groq
from dotenv import load_dotenv

load_dotenv()


class CustomerAgent:
    """
    LLM-powered synthetic customer.
    Drives the conversation toward completing the test intent.
    Tracks conversation history and detects completion/failure.
    """

    def __init__(self, test_case: dict):
        self.client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        self.model = "llama-3.3-70b-versatile"

        self.persona = test_case["persona"]
        self.intent = test_case.get("intent", "Complete a return for my order")
        self.checkpoints = test_case.get("checkpoints", [])
        self.auth_config = test_case.get("auth", {})

        # Conversation history — sent to Groq every turn
        self.conversation_history = []

        # State tracking
        self.auth_triggered = False
        self.intent_complete = False
        self.intent_failed = False
        self.turn_count = 0
        self.max_turns = test_case.get("max_turns", 15)

        print(f"[AGENT] Initialized for intent: {self.intent}")
        print(f"[AGENT] Persona: {self.persona['name']}")

    def _build_system_prompt(self) -> str:
        return f"""You are a customer calling a company's voice AI support line.
You are NOT an AI assistant. You are playing the role of a real human customer.

YOUR PERSONA:
- Name: {self.persona['name']}
- Order Number: {self.persona['order_number']}
- Item: {self.persona['item']}
- Quantity: {self.persona['quantity']}
- Return Reason: {self.persona['return_reason']}

YOUR GOAL: {self.intent}

STRICT RULES:
- Keep ALL responses under 2 sentences — this is a phone call
- Speak naturally like a real person on the phone
- Only give information when directly asked — do not volunteer everything upfront
- If asked for order number give: {self.persona['order_number']}
- If asked for return reason give: {self.persona['return_reason']}
- If the voice AI says it sent a verification email say exactly:
  "Okay, I've verified it" — nothing else
- If the voice AI confirms return is complete say:
  "Thank you, goodbye" then add [INTENT_COMPLETE] at the end
- If the voice AI clearly cannot help or keeps failing after 3 attempts say:
  "Never mind, thank you" then add [INTENT_FAILED] at the end
- Never break character
- Never mention you are an AI or a test
- Never repeat yourself unnecessarily

CURRENT STATE:
- Auth email verified: {self.auth_triggered}
- Turns taken: {self.turn_count}/{self.max_turns}"""

    def get_opening_line(self) -> str:
        """
        What the customer says when the voice AI first greets them.
        Called once at the start of the call.
        """
        messages = [
            {
                "role": "system",
                "content": self._build_system_prompt()
            },
            {
                "role": "user",
                "content": (
                    "Hello, thank you for calling. "
                    "How can I help you today?"
                )
            }
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=100,
            temperature=0.3  # low temp = consistent, predictable responses
        )

        opening = response.choices[0].message.content.strip()
        opening = self._clean_response(opening)

        # Add to history
        self.conversation_history.append({
            "role": "user",
            "content": "Hello, thank you for calling. How can I help you today?"
        })
        self.conversation_history.append({
            "role": "assistant",
            "content": opening
        })

        self.turn_count += 1
        print(f"[AGENT] Opening line: {opening}")
        return opening

    def get_response(
        self,
        voice_ai_utterance: str,
        auth_just_triggered: bool = False
    ) -> tuple[str, bool, bool]:
        """
        Given what the voice AI just said, decide what the customer says next.

        Args:
            voice_ai_utterance: transcribed text from voice AI
            auth_just_triggered: True if auth endpoint was just hit

        Returns:
            (response_text, intent_complete, intent_failed)
        """
        if auth_just_triggered:
            self.auth_triggered = True

        # Check turn limit
        if self.turn_count >= self.max_turns:
            print(f"[AGENT] Max turns reached ({self.max_turns})")
            return "Never mind, thank you", False, True

        # Add voice AI utterance to history
        self.conversation_history.append({
            "role": "user",
            "content": voice_ai_utterance
        })

        messages = [
            {
                "role": "system",
                "content": self._build_system_prompt()
            },
            *self.conversation_history
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=150,
            temperature=0.3
        )

        raw_response = response.choices[0].message.content.strip()

        # Detect completion signals before cleaning
        intent_complete = "[INTENT_COMPLETE]" in raw_response
        intent_failed = "[INTENT_FAILED]" in raw_response

        # Clean signal tokens from spoken response
        clean_response = self._clean_response(raw_response)

        # Add clean response to history
        self.conversation_history.append({
            "role": "assistant",
            "content": clean_response
        })

        self.turn_count += 1

        print(f"[AGENT] Turn {self.turn_count}: {clean_response}")

        if intent_complete:
            print("[AGENT] Intent complete signal received")
            self.intent_complete = True

        if intent_failed:
            print("[AGENT] Intent failed signal received")
            self.intent_failed = True

        return clean_response, intent_complete, intent_failed

    def _clean_response(self, text: str) -> str:
        """Remove signal tokens from text before speaking."""
        return (
            text
            .replace("[INTENT_COMPLETE]", "")
            .replace("[INTENT_FAILED]", "")
            .strip()
        )

    def get_conversation_history(self) -> list:
        """Return full conversation history for evaluator."""
        return self.conversation_history

    def get_transcript_text(self) -> str:
        """
        Return conversation as plain text for LLM evaluation.
        Formats as VOICE_AI / CUSTOMER alternating turns.
        """
        lines = []
        for i, msg in enumerate(self.conversation_history):
            if msg["role"] == "user":
                lines.append(f"VOICE_AI: {msg['content']}")
            else:
                lines.append(f"CUSTOMER: {msg['content']}")
        return "\n".join(lines)