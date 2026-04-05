import socket
import threading
import time
import re
import os
from typing import Callable, Optional


class SIPMessage:
    """
    Parses and builds SIP messages.
    Handles INVITE, ACK, BYE and responses.
    """

    def __init__(self, raw: str):
        self.raw = raw
        self.headers = {}
        self.body = ""
        self.method = None
        self.response_code = None
        self.request_uri = None
        self._parse()  # removed the two wrong lines here

    def _parse(self):
        parts = self.raw.split("\r\n\r\n", 1)
        header_section = parts[0]
        self.body = parts[1] if len(parts) > 1 else ""

        lines = header_section.split("\r\n")
        first_line = lines[0]

        if first_line.startswith("SIP/2.0"):
            tokens = first_line.split(" ", 2)
            self.response_code = int(tokens[1])
        else:
            tokens = first_line.split(" ", 2)
            self.method = tokens[0]
            self.request_uri = tokens[1] if len(tokens) > 1 else ""

        for line in lines[1:]:
            if ":" in line:
                key, _, value = line.partition(":")
                self.headers[key.strip().lower()] = value.strip()

    def get_header(self, name: str) -> Optional[str]:
        return self.headers.get(name.lower())

    def get_rtp_info(self) -> tuple[Optional[str], Optional[int]]:
        rtp_ip = None
        rtp_port = None

        for line in self.body.split("\r\n"):
            if line.startswith("c="):
                parts = line.split()
                if len(parts) >= 3:
                    rtp_ip = parts[-1]
            if line.startswith("m=audio"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        rtp_port = int(parts[1])
                    except ValueError:
                        pass

        return rtp_ip, rtp_port

    def get_call_id(self) -> Optional[str]:
        return self.get_header("call-id")

    def get_from_tag(self) -> Optional[str]:
        from_header = self.get_header("from")
        if from_header:
            match = re.search(r"tag=([^\s;]+)", from_header)
            if match:
                return match.group(1)
        return None

    def get_to_tag(self) -> Optional[str]:
        to_header = self.get_header("to")
        if to_header:
            match = re.search(r"tag=([^\s;]+)", to_header)
            if match:
                return match.group(1)
        return None

    def get_cseq(self) -> Optional[str]:
        return self.get_header("cseq")

    def get_via(self) -> Optional[str]:
        return self.get_header("via")

    def get_contact(self) -> Optional[str]:
        return self.get_header("contact")


class SIPServer:
    def __init__(
        self,
        local_ip: str,
        local_port: int,
        on_call_connected: Callable[[str, int, dict], None],
        on_call_ended: Callable[[str], None]
    ):
        self.local_ip = local_ip
        self.local_port = local_port
        self.on_call_connected = on_call_connected
        self.on_call_ended = on_call_ended

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("0.0.0.0", local_port))
        self.socket.settimeout(1.0)

        self.active_call = None
        self.running = False
        self._thread = None

        print(f"[SIP] Server initialized on {local_ip}:{local_port}")

    def start(self):
        self.running = True
        self._thread = threading.Thread(
            target=self._listen_loop,
            daemon=True
        )
        self._thread.start()
        print("[SIP] Listening for INVITE from Genesys...")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        try:
            self.socket.close()
        except Exception:
            pass
        print("[SIP] Server stopped")

    def send_bye(self):
        if not self.active_call:
            print("[SIP] No active call to hang up")
            return

        call = self.active_call
        bye = self._build_bye(call)
        self.socket.sendto(
            bye.encode(),
            (call["remote_ip"], call["remote_port"])
        )
        print(f"[SIP] BYE sent to {call['remote_ip']}:{call['remote_port']}")
        self.active_call = None

    def _listen_loop(self):
        while self.running:
            try:
                data, addr = self.socket.recvfrom(4096)
                raw = data.decode("utf-8", errors="ignore")
                msg = SIPMessage(raw)
                remote_ip, remote_port = addr

                if msg.method == "INVITE":
                    self._handle_invite(msg, remote_ip, remote_port)
                elif msg.method == "ACK":
                    self._handle_ack(msg)
                elif msg.method == "BYE":
                    self._handle_bye(msg, remote_ip, remote_port)

            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"[SIP] Error: {e}")

    def _handle_invite(self, msg: SIPMessage, remote_ip: str, remote_port: int):
        call_id = msg.get_call_id()
        print(f"[SIP] INVITE received from {remote_ip}:{remote_port}")
        print(f"[SIP] Call-ID: {call_id}")

        rtp_ip, rtp_port = msg.get_rtp_info()
        print(f"[SIP] Genesys RTP endpoint: {rtp_ip}:{rtp_port}")

        self.active_call = {
            "call_id": call_id,
            "from_header": msg.get_header("from"),
            "to_header": msg.get_header("to"),
            "via_header": msg.get_via(),
            "cseq": msg.get_cseq(),
            "remote_ip": remote_ip,
            "remote_port": remote_port,
            "rtp_ip": rtp_ip,
            "rtp_port": rtp_port,
            "local_rtp_port": int(os.getenv("RTP_LOCAL_PORT", "10000"))
        }

        # 100 Trying
        self.socket.sendto(
            self._build_response(msg, 100, "Trying").encode(),
            (remote_ip, remote_port)
        )
        print("[SIP] 100 Trying sent")
        time.sleep(0.1)

        # 180 Ringing
        self.socket.sendto(
            self._build_response(msg, 180, "Ringing").encode(),
            (remote_ip, remote_port)
        )
        print("[SIP] 180 Ringing sent")
        time.sleep(0.5)

        # 200 OK
        self.socket.sendto(
            self._build_200_ok(msg).encode(),
            (remote_ip, remote_port)
        )
        print("[SIP] 200 OK sent — call connected")

        if rtp_ip and rtp_port:
            self.on_call_connected(rtp_ip, rtp_port, self.active_call)
        else:
            print("[SIP] WARNING: Could not extract RTP info from SDP")

    def _handle_ack(self, msg: SIPMessage):
        print("[SIP] ACK received — RTP stream active")

    def _handle_bye(self, msg: SIPMessage, remote_ip: str, remote_port: int):
        print("[SIP] BYE received — call ended by Genesys")
        self.socket.sendto(
            self._build_response(msg, 200, "OK").encode(),
            (remote_ip, remote_port)
        )
        self.active_call = None
        self.on_call_ended(msg.get_call_id())

    def _build_response(self, invite: SIPMessage, code: int, reason: str) -> str:
        return (
            f"SIP/2.0 {code} {reason}\r\n"
            f"Via: {invite.get_via()}\r\n"
            f"From: {invite.get_header('from')}\r\n"
            f"To: {invite.get_header('to')};tag={self._generate_tag()}\r\n"
            f"Call-ID: {invite.get_call_id()}\r\n"
            f"CSeq: {invite.get_cseq()}\r\n"
            f"Content-Length: 0\r\n\r\n"
        )

    def _build_200_ok(self, invite: SIPMessage) -> str:
        local_rtp_port = int(os.getenv("RTP_LOCAL_PORT", "10000"))
        sdp = (
            f"v=0\r\n"
            f"o=voiceai-tester 53655765 2353687637 IN IP4 {self.local_ip}\r\n"
            f"s=VoiceAI Test Session\r\n"
            f"c=IN IP4 {self.local_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {local_rtp_port} RTP/AVP 0 8\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=rtpmap:8 PCMA/8000\r\n"
            f"a=ptime:20\r\n"
        )
        return (
            f"SIP/2.0 200 OK\r\n"
            f"Via: {invite.get_via()}\r\n"
            f"From: {invite.get_header('from')}\r\n"
            f"To: {invite.get_header('to')};tag={self._generate_tag()}\r\n"
            f"Call-ID: {invite.get_call_id()}\r\n"
            f"CSeq: {invite.get_cseq()}\r\n"
            f"Contact: <sip:voiceai-tester@{self.local_ip}:{self.local_port}>\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp)}\r\n"
            f"\r\n"
            f"{sdp}"
        )

    def _build_bye(self, call: dict) -> str:
        return (
            f"BYE sip:genesys@{call['remote_ip']}:{call['remote_port']}"
            f" SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port}"
            f";branch=z9hG4bK{self._generate_tag()}\r\n"
            f"From: <sip:voiceai-tester@{self.local_ip}>;tag=tester\r\n"
            f"To: <sip:genesys@{call['remote_ip']}>\r\n"
            f"Call-ID: {call['call_id']}\r\n"
            f"CSeq: 2 BYE\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n\r\n"
        )

    def _generate_tag(self) -> str:
        import random
        return f"{random.randint(100000, 999999)}"