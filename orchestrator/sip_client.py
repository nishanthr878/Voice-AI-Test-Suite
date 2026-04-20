import socket
import ssl
import random
import os
import time
import hashlib
import re
from typing import Optional


class SIPClient:
    """
    Originates outbound SIP call to a target SIP URI.
    Supports TLS (port 5061) and Digest Authentication.
    """

    def __init__(
        self,
        local_ip: str,
        local_port: int = 5062,
        username: str = None,
        password: str = None
    ):
        self.local_ip = local_ip
        self.local_port = local_port
        self.username = username or os.getenv("SIP_USERNAME")
        self.password = password or os.getenv("SIP_PASSWORD")

        self.call_id = f"{random.randint(100000, 999999)}@{local_ip}"
        self.tag = f"{random.randint(100000, 999999)}"
        self.cseq = 1

        self.remote_ip = None
        self.remote_port = None
        self.remote_tag = None
        self.target_uri = None

        self.rtp_ip = None
        self.rtp_port = None
        self.call_active = False

        # Raw TCP socket — wrapped with TLS
        self._raw_socket = None
        self._tls_socket = None

        print(f"[SIP CLIENT] Initialized on {local_ip}:{local_port}")

    def dial(self, sip_uri: str) -> tuple[Optional[str], Optional[int]]:
        """
        Dial a SIP URI via TLS with digest auth.
        Returns (rtp_ip, rtp_port) on success.
        Returns (None, None) on failure.
        """
        self.target_uri = sip_uri
        clean = sip_uri.replace("sip:", "").replace("sips:", "")

        if "@" in clean:
            user, host_port = clean.split("@", 1)
        else:
            user = "bot"
            host_port = clean

        if ":" in host_port:
            host, port = host_port.rsplit(":", 1)
            port = int(port)
        else:
            host = host_port
            port = 5061

        self.remote_ip = host
        self.remote_port = port

        print(f"[SIP CLIENT] Connecting TLS to {host}:{port}")

        # Establish TLS connection
        if not self._connect_tls(host, port):
            return None, None

        print(f"[SIP CLIENT] TLS connected — sending INVITE")

        # Send initial INVITE
        invite = self._build_invite(sip_uri)
        self._send(invite)

        # Handle response — may need digest auth
        return self._handle_response(sip_uri)

    def _connect_tls(self, host: str, port: int) -> bool:
        """Establish TLS TCP connection to SIP server."""
        try:
            self._raw_socket = socket.socket(
                socket.AF_INET,
                socket.SOCK_STREAM
            )
            self._raw_socket.settimeout(30)

            # Wrap with TLS
            context = ssl.create_default_context()
            # For testing — disable cert verification
            # In production set to ssl.CERT_REQUIRED
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

            self._tls_socket = context.wrap_socket(
                self._raw_socket,
                server_hostname=host
            )
            self._tls_socket.connect((host, port))

            print(f"[SIP CLIENT] TLS handshake complete with {host}:{port}")
            return True

        except Exception as e:
            print(f"[SIP CLIENT] TLS connection failed: {e}")
            return False

    def _send(self, message: str):
        """Send SIP message over TLS socket."""
        try:
            self._tls_socket.sendall(message.encode("utf-8"))
        except Exception as e:
            print(f"[SIP CLIENT] Send error: {e}")

    def _recv(self) -> str:
        """Receive SIP message from TLS socket."""
        try:
            data = b""
            while True:
                chunk = self._tls_socket.recv(4096)
                if not chunk:
                    break
                data += chunk
                # SIP message ends with double CRLF
                if b"\r\n\r\n" in data:
                    # Check if content-length is satisfied
                    raw = data.decode("utf-8", errors="ignore")
                    cl_match = re.search(
                        r"Content-Length:\s*(\d+)",
                        raw,
                        re.IGNORECASE
                    )
                    if cl_match:
                        content_length = int(cl_match.group(1))
                        header_end = data.index(b"\r\n\r\n") + 4
                        if len(data) >= header_end + content_length:
                            break
                    else:
                        break
            return data.decode("utf-8", errors="ignore")
        except socket.timeout:
            return ""
        except Exception as e:
            print(f"[SIP CLIENT] Recv error: {e}")
            return ""

    def _handle_response(
        self,
        sip_uri: str
    ) -> tuple[Optional[str], Optional[int]]:
        """
        Handle SIP responses.
        Handles 407 digest auth challenge automatically.
        """
        while True:
            raw = self._recv()
            if not raw:
                print("[SIP CLIENT] No response received")
                return None, None

            print(f"[SIP CLIENT] Received: {raw.split(chr(13))[0]}")

            if "100 Trying" in raw:
                print("[SIP CLIENT] 100 Trying")
                continue

            elif "180 Ringing" in raw:
                print("[SIP CLIENT] 180 Ringing — voice AI answering")
                continue

            elif "200 OK" in raw:
                print("[SIP CLIENT] 200 OK — call connected")

                # Extract remote tag
                for line in raw.split("\r\n"):
                    if line.lower().startswith("to:") and "tag=" in line:
                        self.remote_tag = line.split("tag=")[-1].strip()

                # Extract RTP info
                rtp_ip, rtp_port = self._parse_sdp(raw)
                self.rtp_ip = rtp_ip
                self.rtp_port = rtp_port
                print(f"[SIP CLIENT] Voice AI RTP: {rtp_ip}:{rtp_port}")

                # Send ACK
                ack = self._build_ack()
                self._send(ack)
                print("[SIP CLIENT] ACK sent — audio starting")

                self.call_active = True
                return rtp_ip, rtp_port

            elif "407 Proxy Authentication Required" in raw or \
                 "401 Unauthorized" in raw:
                print("[SIP CLIENT] Auth challenge received — responding")

                # Extract challenge
                auth_header = self._extract_auth_challenge(raw)
                if not auth_header:
                    print("[SIP CLIENT] Could not parse auth challenge")
                    return None, None

                # Rebuild INVITE with credentials
                self.cseq += 1
                invite_with_auth = self._build_invite_with_auth(
                    sip_uri,
                    auth_header,
                    "407" in raw
                )
                self._send(invite_with_auth)
                print("[SIP CLIENT] Re-sent INVITE with credentials")
                continue

            elif "403 Forbidden" in raw:
                print("[SIP CLIENT] 403 Forbidden — wrong credentials")
                return None, None

            elif "404 Not Found" in raw:
                print("[SIP CLIENT] 404 — SIP URI not found")
                return None, None

            elif "486 Busy" in raw:
                print("[SIP CLIENT] 486 Busy")
                return None, None

            else:
                # Unknown response
                status = raw.split("\r\n")[0] if raw else "unknown"
                print(f"[SIP CLIENT] Unexpected response: {status}")
                return None, None

    def _extract_auth_challenge(self, raw: str) -> Optional[dict]:
        """
        Extract digest auth parameters from 407/401 response.
        Parses: WWW-Authenticate or Proxy-Authenticate header.
        """
        auth_line = None
        for line in raw.split("\r\n"):
            if line.lower().startswith("proxy-authenticate:") or \
               line.lower().startswith("www-authenticate:"):
                auth_line = line
                break

        if not auth_line:
            return None

        # Parse digest params
        params = {}
        for match in re.finditer(r'(\w+)="([^"]*)"', auth_line):
            params[match.group(1)] = match.group(2)
        for match in re.finditer(r'(\w+)=([^",\s]+)', auth_line):
            if match.group(1) not in params:
                params[match.group(1)] = match.group(2)

        return params

    def _build_digest_response(
        self,
        auth_params: dict,
        method: str,
        uri: str
    ) -> str:
        """
        Build digest auth response header.
        RFC 3261 digest authentication.
        """
        realm = auth_params.get("realm", "")
        nonce = auth_params.get("nonce", "")
        algorithm = auth_params.get("algorithm", "MD5")
        qop = auth_params.get("qop", "")

        # HA1 = MD5(username:realm:password)
        ha1 = hashlib.md5(
            f"{self.username}:{realm}:{self.password}".encode()
        ).hexdigest()

        # HA2 = MD5(method:uri)
        ha2 = hashlib.md5(
            f"{method}:{uri}".encode()
        ).hexdigest()

        if qop:
            # With qop
            nc = "00000001"
            cnonce = hashlib.md5(
                str(random.randint(0, 999999)).encode()
            ).hexdigest()[:8]
            response = hashlib.md5(
                f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
            ).hexdigest()

            return (
                f'Digest username="{self.username}", '
                f'realm="{realm}", '
                f'nonce="{nonce}", '
                f'uri="{uri}", '
                f'algorithm={algorithm}, '
                f'qop={qop}, '
                f'nc={nc}, '
                f'cnonce="{cnonce}", '
                f'response="{response}"'
            )
        else:
            # Without qop
            response = hashlib.md5(
                f"{ha1}:{nonce}:{ha2}".encode()
            ).hexdigest()

            return (
                f'Digest username="{self.username}", '
                f'realm="{realm}", '
                f'nonce="{nonce}", '
                f'uri="{uri}", '
                f'algorithm={algorithm}, '
                f'response="{response}"'
            )

    def _build_invite(self, sip_uri: str) -> str:
        """Build initial SIP INVITE without auth."""
        local_rtp_port = int(os.getenv("RTP_LOCAL_PORT", "10000"))

        sdp = (
            f"v=0\r\n"
            f"o=voiceai-tester 53655765 2353687637 "
            f"IN IP4 {self.local_ip}\r\n"
            f"s=VoiceAI Test\r\n"
            f"c=IN IP4 {self.local_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {local_rtp_port} RTP/AVP 0 8\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=rtpmap:8 PCMA/8000\r\n"
            f"a=ptime:20\r\n"
            f"a=sendrecv\r\n"
        )

        return (
            f"INVITE {sip_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/TLS {self.local_ip}:{self.local_port}"
            f";branch=z9hG4bK{random.randint(100000, 999999)}\r\n"
            f"Max-Forwards: 70\r\n"
            f"From: <sip:{self.username}@{self.local_ip}>"
            f";tag={self.tag}\r\n"
            f"To: <{sip_uri}>\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} INVITE\r\n"
            f"Contact: <sip:{self.username}@"
            f"{self.local_ip}:{self.local_port};transport=tls>\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp)}\r\n"
            f"\r\n"
            f"{sdp}"
        )

    def _build_invite_with_auth(
        self,
        sip_uri: str,
        auth_params: dict,
        is_proxy_auth: bool = True
    ) -> str:
        """Build INVITE with digest auth credentials."""
        local_rtp_port = int(os.getenv("RTP_LOCAL_PORT", "10000"))

        sdp = (
            f"v=0\r\n"
            f"o=voiceai-tester 53655765 2353687637 "
            f"IN IP4 {self.local_ip}\r\n"
            f"s=VoiceAI Test\r\n"
            f"c=IN IP4 {self.local_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {local_rtp_port} RTP/AVP 0 8\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=rtpmap:8 PCMA/8000\r\n"
            f"a=ptime:20\r\n"
            f"a=sendrecv\r\n"
        )

        auth_response = self._build_digest_response(
            auth_params, "INVITE", sip_uri
        )

        # Use Proxy-Authorization for 407, Authorization for 401
        auth_header_name = (
            "Proxy-Authorization" if is_proxy_auth
            else "Authorization"
        )

        return (
            f"INVITE {sip_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/TLS {self.local_ip}:{self.local_port}"
            f";branch=z9hG4bK{random.randint(100000, 999999)}\r\n"
            f"Max-Forwards: 70\r\n"
            f"From: <sip:{self.username}@{self.local_ip}>"
            f";tag={self.tag}\r\n"
            f"To: <{sip_uri}>\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} INVITE\r\n"
            f"Contact: <sip:{self.username}@"
            f"{self.local_ip}:{self.local_port};transport=tls>\r\n"
            f"{auth_header_name}: {auth_response}\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp)}\r\n"
            f"\r\n"
            f"{sdp}"
        )

    def _build_ack(self) -> str:
        return (
            f"ACK {self.target_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/TLS {self.local_ip}:{self.local_port}"
            f";branch=z9hG4bK{random.randint(100000, 999999)}\r\n"
            f"Max-Forwards: 70\r\n"
            f"From: <sip:{self.username}@{self.local_ip}>"
            f";tag={self.tag}\r\n"
            f"To: <{self.target_uri}>;tag={self.remote_tag or ''}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} ACK\r\n"
            f"Content-Length: 0\r\n\r\n"
        )

    def _build_bye(self) -> str:
        self.cseq += 1
        return (
            f"BYE {self.target_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/TLS {self.local_ip}:{self.local_port}"
            f";branch=z9hG4bK{random.randint(100000, 999999)}\r\n"
            f"Max-Forwards: 70\r\n"
            f"From: <sip:{self.username}@{self.local_ip}>"
            f";tag={self.tag}\r\n"
            f"To: <{self.target_uri}>;tag={self.remote_tag or ''}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} BYE\r\n"
            f"Content-Length: 0\r\n\r\n"
        )

    def hangup(self):
        if not self.call_active:
            print("[SIP CLIENT] No active call")
            return
        try:
            bye = self._build_bye()
            self._send(bye)
            print("[SIP CLIENT] BYE sent")
            self.call_active = False
        except Exception as e:
            print(f"[SIP CLIENT] Hangup error: {e}")
        finally:
            try:
                self._tls_socket.close()
            except Exception:
                pass

    def _parse_sdp(self, raw: str) -> tuple[str, int]:
        rtp_ip = self.remote_ip
        rtp_port = 20000

        parts = raw.split("\r\n\r\n", 1)
        if len(parts) < 2:
            return rtp_ip, rtp_port

        body = parts[1]
        for line in body.split("\r\n"):
            if line.startswith("c="):
                rtp_ip = line.split()[-1]
            if line.startswith("m=audio"):
                parts_m = line.split()
                if len(parts_m) >= 2:
                    try:
                        rtp_port = int(parts_m[1])
                    except ValueError:
                        pass

        return rtp_ip, rtp_port