"""Real Oracle Net (TNS) handshake.

Sends a Connect packet (Type=01) and parses the listener's first response.
Any of Accept (02) / Refuse (04) / Redirect (05) / Resend (0B) confirms that
Oracle Net is alive on the wire — the listener may legitimately Refuse with
'no service' until Agent 4 creates the database, and that still counts as
proof that we can speak Oracle Net to the SCAN.
"""
from __future__ import annotations

import socket
import ssl
import struct
from typing import Any

from .common import CheckResult, Severity, timed

TNS_TYPE_NAMES = {
    1: "Connect",
    2: "Accept",
    4: "Refuse",
    5: "Redirect",
    6: "Data",
    11: "Resend",
    12: "Marker",
}

# A response of one of these proves Oracle Net is alive
TNS_LIVE_TYPES = {2, 4, 5, 11}


def _build_connect_packet(host: str, port: int, service_name: str | None) -> bytes:
    cd_str = (
        f"(DESCRIPTION="
        f"(ADDRESS=(PROTOCOL=TCP)(HOST={host})(PORT={port}))"
        f"(CONNECT_DATA="
        f"(CID=(PROGRAM=agent3-oranet)(HOST=__agent3__)(USER=__probe__))"
        + (f"(SERVICE_NAME={service_name})" if service_name else "(SERVICE_NAME=NONEXISTENT_PROBE)")
        + "))"
    )
    cd = cd_str.encode("ascii")
    cd_len = len(cd)

    header_len = 58
    pkt_len = header_len + cd_len

    pkt = b""
    pkt += struct.pack(">I", pkt_len)        # 0:4   packet length
    pkt += struct.pack(">H", 0)              # 4:6   packet checksum
    pkt += struct.pack(">B", 1)              # 6     type = Connect
    pkt += struct.pack(">B", 0)              # 7     reserved
    pkt += struct.pack(">H", 0)              # 8:10  header checksum
    pkt += struct.pack(">H", 0x0139)         # 10:12 version (313 — 19c family)
    pkt += struct.pack(">H", 0x012c)         # 12:14 version compat (300)
    pkt += struct.pack(">H", 0x0c41)         # 14:16 service options
    pkt += struct.pack(">H", 0x2000)         # 16:18 SDU
    pkt += struct.pack(">H", 0x7fff)         # 18:20 TDU
    pkt += struct.pack(">H", 0x4f98)         # 20:22 protocol characteristics
    pkt += struct.pack(">H", 0)              # 22:24 line turnaround
    pkt += struct.pack(">H", 1)              # 24:26 value of one
    pkt += struct.pack(">H", cd_len)         # 26:28 length of connect data
    pkt += struct.pack(">H", header_len)     # 28:30 offset to connect data
    pkt += struct.pack(">I", 0x00000800)     # 30:34 max recv data
    pkt += struct.pack(">B", 0x41)           # 34    connect flags 0
    pkt += struct.pack(">B", 0x41)           # 35    connect flags 1
    pkt += struct.pack(">I", 0)              # 36:40 trace cross id
    pkt += struct.pack(">I", 0)              # 40:44 trace unique id
    pkt += struct.pack(">I", 0)              # 44:48 session id
    pkt += b"\x00" * 10                      # 48:58 reserved
    pkt += cd
    return pkt


def _parse_response(data: bytes) -> dict[str, Any]:
    if len(data) < 8:
        return {"ok": False, "reason": f"short response ({len(data)} bytes)", "raw_hex": data.hex()}
    pkt_len = struct.unpack(">I", data[0:4])[0]
    pkt_type = data[4]
    info: dict[str, Any] = {
        "type_code": pkt_type,
        "type_name": TNS_TYPE_NAMES.get(pkt_type, f"Unknown(0x{pkt_type:02x})"),
        "advertised_length": pkt_len,
        "received_length": len(data),
        "ok": pkt_type in TNS_LIVE_TYPES,
        "raw_hex": data[: min(96, len(data))].hex(),
    }
    # Pull a human-readable refuse reason if present
    if pkt_type == 4:
        try:
            tail = data[8:].decode("ascii", errors="ignore")
            if "ERROR_STACK" in tail or "ERR=" in tail:
                info["refuse_text"] = tail.strip("\x00 \r\n")[:512]
        except Exception:
            pass
    return info


def _handshake(host: str, port: int, *, tls: bool, timeout: float = 8.0,
               service_name: str | None = None) -> tuple[Severity, str, dict[str, Any]]:
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if tls:
            ctx = ssl.create_default_context()
            # Oracle's SCAN TCPS cert may be self-signed in fresh deployments.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.settimeout(timeout)
        sock.sendall(_build_connect_packet(host, port, service_name))
        # Read at least the 8-byte header
        chunks: list[bytes] = []
        deadline_reads = 4
        sock.settimeout(timeout)
        for _ in range(deadline_reads):
            try:
                buf = sock.recv(4096)
            except socket.timeout:
                break
            if not buf:
                break
            chunks.append(buf)
            if sum(len(c) for c in chunks) >= 8:
                break
        data = b"".join(chunks)
    finally:
        try:
            sock.close()
        except Exception:
            pass

    info = _parse_response(data)
    if info["ok"]:
        return Severity.PASS, f"TNS {info['type_name']} from {host}:{port}", info
    if info.get("type_code") is not None:
        return Severity.WARN, f"TNS unexpected type {info['type_name']}", info
    return Severity.FAIL, f"no TNS response: {info.get('reason')}", info


@timed
def _probe_tns(host: str, port: int, tls: bool, label: str) -> CheckResult:
    name = f"oracle_net.{label}"
    try:
        sev, summary, details = _handshake(host, port, tls=tls)
        return CheckResult(name, sev, summary, details, target=f"{host}:{port}")
    except (socket.timeout, ConnectionRefusedError, OSError, ssl.SSLError) as e:
        return CheckResult(
            name, Severity.FAIL,
            f"handshake failed {host}:{port} ({type(e).__name__}: {e})",
            target=f"{host}:{port}",
        )


def check_oracle_net_handshake(payload: dict[str, Any]) -> list[CheckResult]:
    scan = payload["oracle_net"]["scan_dns_name"]
    tcp_port = int(payload["oracle_net"]["scan_listener_port_tcp"])
    tcps_port = int(payload["oracle_net"].get("scan_listener_port_tcp_ssl") or 2484)
    return [
        _probe_tns(scan, tcp_port, tls=False, label=f"scan-tcp-{tcp_port}"),
        _probe_tns(scan, tcps_port, tls=True, label=f"scan-tcps-{tcps_port}"),
    ]
