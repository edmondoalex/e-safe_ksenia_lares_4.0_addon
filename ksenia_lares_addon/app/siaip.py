import json
import re
import socketserver
import threading
import time


def _crc16_sia(data: bytes) -> int:
    # SIA DC-09 uses the CRC-16/IBM bit order (poly 0xA001, init 0x0000).
    # Ksenia frames in the field match this variant.
    crc = 0x0000
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x0001:
                crc = ((crc >> 1) ^ 0xA001) & 0xFFFF
            else:
                crc = (crc >> 1) & 0xFFFF
    return crc & 0xFFFF


def _build_frame(data: str) -> bytes:
    body = str(data or "").encode("ascii", errors="ignore")
    crc = _crc16_sia(body)
    return b"\n" + f"{crc:04X}{len(body):04X}".encode("ascii") + body + b"\r"


def _extract_dc09_data(raw: bytes) -> tuple[str, str]:
    text = raw.decode("ascii", errors="ignore").strip("\x00\r\n ")
    if not text:
        return "", ""
    if text.startswith('"'):
        return text, ""
    m = re.match(r"^[0-9A-Fa-f]{4}([0-9A-Fa-f]{4})(.*)$", text, re.S)
    if not m:
        return text, ""
    try:
        expected_len = int(m.group(1), 16)
    except Exception:
        expected_len = 0
    data = m.group(2)
    if expected_len > 0 and len(data) >= expected_len:
        data = data[:expected_len]
    return data, text


def parse_sia_event(raw: bytes, remote: str = "") -> dict:
    data, framed = _extract_dc09_data(raw)
    now = time.time()
    event = {
        "ID": str(int(now * 1000)),
        "ts": now,
        "remote": remote,
        "raw": raw.decode("ascii", errors="replace").strip("\r\n"),
        "frame": framed,
        "data": data,
        "protocol": "",
        "sequence": "",
        "routing": "",
        "account": "",
        "code": "",
        "qualifier": "",
        "zone": "",
        "partition": "",
        "description": "",
        "category": "unknown",
    }
    if not data:
        event["description"] = "Pacchetto SIA-IP vuoto/non riconosciuto"
        return event

    m = re.match(r'^"([^"]+)"(\d{4})', data)
    if m:
        event["protocol"] = m.group(1)
        event["sequence"] = m.group(2)
        try:
            rest = data[m.end():]
            hash_pos = rest.find("#")
            bracket_pos = rest.find("[")
            cut_positions = [p for p in (hash_pos, bracket_pos) if p >= 0]
            if cut_positions:
                event["routing"] = rest[: min(cut_positions)]
            else:
                event["routing"] = rest
        except Exception:
            event["routing"] = ""

    m = re.search(r"#([A-Za-z0-9_-]+)", data)
    if m:
        event["account"] = m.group(1)

    content = ""
    m = re.search(r"\[(.*)\]", data)
    if m:
        content = m.group(1)
    else:
        content = data

    # Common SIA-DCS payloads contain pieces such as /BA001, /BR001, /TA000.
    m = re.search(r"/([A-Z]{2})([A-Za-z0-9]{0,8})", content)
    if not m:
        m = re.search(r"\b([A-Z]{2})(\d{0,8})\b", content)
    if m:
        event["code"] = m.group(1).upper()
        ident = str(m.group(2) or "").strip()
        if ident:
            event["zone"] = ident.lstrip("0") or "0"
    else:
        # SIA-DCS often uses a qualifier before the two-letter code, e.g.
        # |NJP1^installatore^ or |NRP. Keep the qualifier and decode JP/RP.
        qm = re.search(r"\|([A-Z])([A-Z]{2})([A-Za-z0-9]*)(?:\^([^^]*)\^)?", content)
        if qm:
            event["qualifier"] = qm.group(1).upper()
            event["code"] = qm.group(2).upper()
            ident = str(qm.group(3) or "").strip()
            if ident:
                event["zone"] = ident.lstrip("0") or "0"
            if qm.group(4):
                event["user"] = str(qm.group(4)).strip()

    # Partition hints vary by panel; keep this permissive.
    pm = re.search(r"(?:ri|pi|partition|partizione)\s*[:=/ ]\s*0*(\d+)", content, re.I)
    if pm:
        event["partition"] = str(int(pm.group(1)))

    code = event["code"]
    descriptions = {
        "BA": ("alarm", "Allarme furto"),
        "BR": ("restore", "Ripristino allarme furto"),
        "FA": ("alarm", "Allarme incendio"),
        "FR": ("restore", "Ripristino allarme incendio"),
        "PA": ("alarm", "Allarme panico"),
        "PR": ("restore", "Ripristino allarme panico"),
        "HA": ("alarm", "Allarme rapina"),
        "HR": ("restore", "Ripristino allarme rapina"),
        "TA": ("tamper", "Sabotaggio"),
        "TR": ("restore", "Ripristino sabotaggio"),
        "AT": ("trouble", "Guasto alimentazione"),
        "AR": ("restore", "Ripristino alimentazione"),
        "YT": ("trouble", "Guasto sistema"),
        "YR": ("restore", "Ripristino guasto sistema"),
        "CL": ("arm", "Inserimento"),
        "OP": ("disarm", "Disinserimento"),
        "JP": ("user", "Accesso utente"),
        "RP": ("test", "Test comunicazione"),
    }
    cat, desc = descriptions.get(code, ("unknown", f"Evento SIA {code}" if code else "Evento SIA-IP"))
    event["category"] = cat
    event["description"] = desc
    if code in ("OP", "CL", "JP") and event.get("zone"):
        event["user_id"] = event.get("zone")
        if event.get("user"):
            event["user_name"] = event.get("user")
    return event


def build_ack(event: dict) -> bytes:
    sequence = str((event or {}).get("sequence") or "0000")
    if not re.match(r"^\d{4}$", sequence):
        sequence = "0000"
    account = str((event or {}).get("account") or "").strip()
    routing = str((event or {}).get("routing") or "").strip()
    acct_part = f"#{account}" if account else ""
    # DC-09 ACK for unencrypted SIA-DCS style messages.
    return _build_frame(f'"ACK"{sequence}{routing}{acct_part}[]')


class _SiaTcpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        remote = ""
        try:
            remote = self.client_address[0]
        except Exception:
            remote = ""
        chunks = []
        self.request.settimeout(2.0)
        while True:
            try:
                data = self.request.recv(4096)
            except Exception:
                break
            if not data:
                break
            chunks.append(data)
            if b"\r" in data or b"\n" in data:
                break
        raw = b"".join(chunks)
        if not raw:
            return
        event = parse_sia_event(raw, remote=remote)
        try:
            self.request.sendall(build_ack(event))
            event["ack_sent"] = True
        except Exception as exc:
            event["ack_sent"] = False
            event["ack_error"] = str(exc)
        try:
            server.on_event(event)
        except Exception:
            pass
        try:
            if getattr(server, "debug", False):
                server.logger.info("SIA-IP event: %s", json.dumps(event, ensure_ascii=False))
        except Exception:
            pass


class _ThreadingSiaServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, on_event, logger, debug=False):
        super().__init__(address, handler)
        self.on_event = on_event
        self.logger = logger
        self.debug = bool(debug)


class SiaIpReceiver:
    def __init__(self, host: str, port: int, on_event, logger, debug: bool = False):
        self.host = str(host or "0.0.0.0")
        self.port = int(port)
        self.on_event = on_event
        self.logger = logger
        self.debug = bool(debug)
        self._server = None
        self._thread = None

    def start(self):
        if self._server is not None:
            return
        self._server = _ThreadingSiaServer(
            (self.host, self.port),
            _SiaTcpHandler,
            self.on_event,
            self.logger,
            self.debug,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="sia_ip_receiver",
            daemon=True,
        )
        self._thread.start()
        self.logger.info("SIA-IP receiver listening on %s:%s/tcp", self.host, self.port)

    def stop(self):
        srv = self._server
        self._server = None
        if srv is None:
            return
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass
