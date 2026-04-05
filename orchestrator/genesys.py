import os
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()


class GenesysClient:
    """
    Handles all Genesys Cloud API interactions.
    - OAuth2 token management (auto-refresh)
    - Outbound call trigger
    """

    def __init__(self):
        self.client_id = os.getenv("GENESYS_CLIENT_ID")
        self.client_secret = os.getenv("GENESYS_CLIENT_SECRET")
        self.region = os.getenv("GENESYS_REGION", "mypurecloud.com")
        self.queue_id = os.getenv("GENESYS_QUEUE_ID")
        self.caller_id = os.getenv("GENESYS_CALLER_ID")

        # actuall endpoint for gensys
        # self.base_url = f"https://api.{self.region}"
        # self.auth_url = f"https://login.{self.region}/oauth/token"

        # mocking for testing
        self.base_url = f"http://{self.region}"
        self.auth_url = f"http://{self.region}/oauth/token"

        # Token cache
        self._token = None
        self._token_expires_at = None

    def _get_token(self) -> str:
        """
        Gets OAuth2 token using client credentials flow.
        Auto-refreshes when expired.
        """
        # Return cached token if still valid
        if self._token and datetime.now() < self._token_expires_at:
            return self._token

        print("[GENESYS] Fetching new OAuth2 token")

        response = requests.post(
            self.auth_url,
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

        if response.status_code != 200:
            raise Exception(
                f"Genesys auth failed: {response.status_code} {response.text}"
            )

        data = response.json()
        self._token = data["access_token"]

        # Cache token with 60 second buffer before expiry
        expires_in = data.get("expires_in", 3600)
        self._token_expires_at = datetime.now() + timedelta(
            seconds=expires_in - 60
        )

        print(f"[GENESYS] Token obtained, expires in {expires_in}s")
        return self._token

    def trigger_outbound_call(self, phone_number: str) -> dict:
        """
        Tells Genesys to:
        1. Dial the target phone number (voice AI under test)
        2. Bridge audio to our SIP endpoint

        Returns the conversation ID for tracking.
        """
        token = self._get_token()

        print(f"[GENESYS] Triggering outbound call to {phone_number}")

        response = requests.post(
            f"{self.base_url}/api/v2/conversations/calls",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={
                "phoneNumber": phone_number,
                "callFromQueueId": self.queue_id,
                "callerId": self.caller_id,
            }
        )

        if response.status_code not in (200, 201):
            raise Exception(
                f"Genesys call trigger failed: "
                f"{response.status_code} {response.text}"
            )

        data = response.json()
        conversation_id = data.get("id")
        print(f"[GENESYS] Call triggered, conversation ID: {conversation_id}")

        return {
            "conversation_id": conversation_id,
            "phone_number": phone_number,
            "raw": data
        }

    def get_conversation(self, conversation_id: str) -> dict:
        """
        Fetch conversation details — useful for post-call metadata.
        """
        token = self._get_token()

        response = requests.get(
            f"{self.base_url}/api/v2/conversations/{conversation_id}",
            headers={"Authorization": f"Bearer {token}"}
        )

        if response.status_code != 200:
            raise Exception(
                f"Failed to fetch conversation: "
                f"{response.status_code} {response.text}"
            )

        return response.json()

    def end_conversation(self, conversation_id: str, participant_id: str):
        """
        Hangs up the call — called when agent signals INTENT_COMPLETE.
        """
        token = self._get_token()

        response = requests.patch(
            f"{self.base_url}/api/v2/conversations/calls/"
            f"{conversation_id}/participants/{participant_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={"state": "disconnected"}
        )

        print(f"[GENESYS] Call ended: {response.status_code}")

    def verify_credentials(self) -> bool:
        """
        Quick check to verify credentials work.
        Call this on startup before running any tests.
        """
        try:
            self._get_token()
            print("[GENESYS] Credentials verified successfully")
            return True
        except Exception as e:
            print(f"[GENESYS] Credential verification failed: {e}")
            return False