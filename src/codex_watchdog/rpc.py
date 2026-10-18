"""Bounded, synchronous JSON-RPC over a local Unix WebSocket (stdlib only)."""
import base64
import hashlib
import json
import os
import socket
import struct
import time


class RpcError(RuntimeError):
    pass


class Client:
    MAX_MESSAGE = 8 * 1024 * 1024

    def __init__(self, path, timeout=5):
        self.timeout = timeout
        self.next_id = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(path)
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall((
                "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Key: {key}\r\n\r\n"
            ).encode())
            header = bytearray()
            deadline = time.monotonic() + timeout
            while not header.endswith(b"\r\n\r\n"):
                if len(header) >= 16384:
                    raise RpcError("oversized handshake")
                header.extend(self._read(1, deadline))
            lines = bytes(header).decode("ascii").split("\r\n")
            fields = dict(line.lower().split(":", 1) for line in lines[1:] if ":" in line)
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()).decode()
            # Header names are case-insensitive; base64 values are not.
            actual = next((line.split(":", 1)[1].strip() for line in lines[1:]
                           if line.lower().startswith("sec-websocket-accept:")), None)
            if (lines[0].split()[1:2] != ["101"] or actual != accept
                    or fields.get("upgrade", "").strip() != "websocket"):
                raise RpcError("invalid Unix WebSocket handshake")
            self.request("initialize", {
                "clientInfo": {"name": "codex_capacity_watchdog", "version": "1.0.0"}
            })
            self.send({"method": "initialized"})
        except BaseException:
            self.close()
            raise

    def close(self):
        self.sock.close()

    def _read(self, size, deadline):
        result = bytearray()
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RPC deadline exceeded")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(size - len(result))
            if not chunk:
                raise EOFError("daemon disconnected")
            result.extend(chunk)
        return bytes(result)

    def _frame(self, opcode, data):
        size = len(data)
        if size > self.MAX_MESSAGE:
            raise RpcError("outgoing message too large")
        mask = os.urandom(4)
        if size < 126:
            prefix = bytes([0x80 | opcode, 0x80 | size])
        elif size <= 65535:
            prefix = bytes([0x80 | opcode, 254]) + struct.pack("!H", size)
        else:
            prefix = bytes([0x80 | opcode, 255]) + struct.pack("!Q", size)
        self.sock.settimeout(self.timeout)
        self.sock.sendall(prefix + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, message):
        self._frame(1, json.dumps(message, separators=(",", ":")).encode())

    def receive(self, deadline):
        payload = bytearray()
        fragmented = False
        while True:
            a, b = self._read(2, deadline)
            final, opcode, size = bool(a & 128), a & 15, b & 127
            if a & 112 or b & 128:
                raise RpcError("unsupported WebSocket flags")
            if size == 126:
                size = struct.unpack("!H", self._read(2, deadline))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8, deadline))[0]
            if size + len(payload) > self.MAX_MESSAGE:
                raise RpcError("incoming message too large")
            if opcode >= 8 and (not final or size > 125):
                raise RpcError("invalid control frame")
            data = self._read(size, deadline)
            if opcode == 8:
                raise EOFError("daemon closed WebSocket")
            if opcode == 9:
                self._frame(10, data)
                continue
            if opcode == 10:
                continue
            if opcode == 1 and not fragmented:
                fragmented = True
            elif opcode != 0 or not fragmented:
                raise RpcError("unexpected WebSocket opcode")
            payload.extend(data)
            if final:
                result = json.loads(payload)
                if not isinstance(result, dict):
                    raise RpcError("invalid RPC message")
                return result

    def request(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            message = self.receive(deadline)
            if "method" in message:
                # Never answer approval or tool requests. This is an observer,
                # not a subscriber or execution client.
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RpcError(f"{method}: {message['error']}")
            if "result" not in message:
                raise RpcError("missing RPC result")
            return message["result"]
