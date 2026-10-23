"""Local-only Ollama HTTP client with cancellable NDJSON streaming."""
from __future__ import annotations
from contextlib import contextmanager
import json
import socket
import threading
from typing import Callable


class Ollama:
    def __init__(self, host="127.0.0.1", port=11434):
        self.host, self.port = host, port
        self._active: socket.socket | None = None
        self._lock = threading.Lock()

    def cancel(self):
        with self._lock:
            active = self._active
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            active.close()

    @contextmanager
    def _response(self, route, body=None, stop: threading.Event | None = None):
        """Return socket, status, headers and an iterator over decoded HTTP body chunks."""
        if stop and stop.is_set():
            raise InterruptedError("Ollama request stopped")
        sock = socket.create_connection((self.host, self.port), timeout=30)
        sock.settimeout(0.5)
        with self._lock:
            self._active = sock
        try:
            payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
            method = "GET" if body is None else "POST"
            headers = (f"{method} /api{route} HTTP/1.1\r\n"
                       f"Host: {self.host}:{self.port}\r\n"
                       "Connection: close\r\n"
                       "Content-Type: application/json\r\n"
                       f"Content-Length: {len(payload)}\r\n\r\n").encode("ascii")
            sock.sendall(headers + payload)
            buffer = bytearray()

            def receive():
                while True:
                    if stop and stop.is_set():
                        raise InterruptedError("Ollama request stopped")
                    try:
                        return sock.recv(65536)
                    except socket.timeout:
                        continue
                    except OSError as exc:
                        if stop and stop.is_set():
                            raise InterruptedError("Ollama request stopped") from exc
                        raise

            def until(delimiter: bytes, maximum=65536):
                while True:
                    index = buffer.find(delimiter)
                    if index >= 0:
                        data = bytes(buffer[:index])
                        del buffer[:index + len(delimiter)]
                        return data
                    if len(buffer) > maximum:
                        raise RuntimeError("Ollama HTTP header or chunk line too large")
                    part = receive()
                    if not part:
                        raise RuntimeError("Ollama closed the connection unexpectedly")
                    buffer.extend(part)

            def take(count: int):
                while len(buffer) < count:
                    part = receive()
                    if not part:
                        raise RuntimeError("Ollama closed the response early")
                    buffer.extend(part)
                part = bytes(buffer[:count])
                del buffer[:count]
                return part

            head = until(b"\r\n\r\n")
            lines = head.decode("iso-8859-1").split("\r\n")
            status_line = lines[0].split(" ", 2)
            if len(status_line) < 2 or not status_line[1].isdigit():
                raise RuntimeError("Invalid Ollama HTTP response")
            status = int(status_line[1])
            response_headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    response_headers[key.lower()] = value.strip()

            def body_chunks():
                if "chunked" in response_headers.get("transfer-encoding", "").lower():
                    while True:
                        line = until(b"\r\n", maximum=128)
                        try:
                            size = int(line.split(b";", 1)[0], 16)
                        except ValueError as exc:
                            raise RuntimeError("Invalid Ollama HTTP chunk") from exc
                        if size == 0:
                            break
                        remaining = size
                        while remaining:
                            part = take(min(remaining, 65536))
                            remaining -= len(part)
                            yield part
                        if take(2) != b"\r\n":
                            raise RuntimeError("Invalid Ollama HTTP chunk ending")
                elif "content-length" in response_headers:
                    remaining = int(response_headers["content-length"])
                    while remaining:
                        part = take(min(remaining, 65536))
                        remaining -= len(part)
                        yield part
                else:
                    if buffer:
                        yield bytes(buffer)
                        buffer.clear()
                    while True:
                        part = receive()
                        if not part:
                            break
                        yield part

            yield status, body_chunks()
        finally:
            with self._lock:
                if self._active is sock:
                    self._active = None
            sock.close()

    def request(self, route, body=None, stop=None):
        with self._response(route, body, stop) as (status, chunks):
            contents = b"".join(chunks)
        if status >= 400:
            raise RuntimeError(f"Ollama {route}: HTTP {status} {contents[:400].decode('utf-8', 'replace')}")
        return json.loads(contents)

    def stream(self, route, body, on_item: Callable, stop=None):
        with self._response(route, body, stop) as (status, chunks):
            pending = bytearray()
            last = None
            error_text = bytearray()
            for chunk in chunks:
                if status >= 400:
                    error_text.extend(chunk[:400 - len(error_text)])
                    continue
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    if line.strip():
                        item = json.loads(line)
                        if item.get("error"):
                            raise RuntimeError(item["error"])
                        last = item
                        on_item(item)
            if status >= 400:
                raise RuntimeError(f"Ollama {route}: HTTP {status} {error_text.decode('utf-8', 'replace')}")
            if pending.strip():
                item = json.loads(pending)
                if item.get("error"):
                    raise RuntimeError(item["error"])
                last = item
                on_item(item)
            return last

    def models(self):
        return self.request("/tags")

    def running(self):
        return self.request("/ps")

    def version(self):
        return self.request("/version")

    def show(self, model):
        return self.request("/show", {"model": model})

    def unload(self, model):
        return self.request("/generate", {"model": model, "prompt": "", "stream": False,
                                          "keep_alive": 0})

    def preload(self, model, stop=None):
        return self.request("/generate", {"model": model, "prompt": "", "stream": False,
                                          "keep_alive": "10m"}, stop)

    def generate(self, model, prompt, think, options, stop=None):
        body = {"model": model, "prompt": prompt, "stream": True, "keep_alive": "10m",
                "options": options}
        if think is not None:
            body["think"] = think
        responses, thoughts, final = [], [], None
        def collect(item):
            nonlocal final
            responses.append(item.get("response") or "")
            thoughts.append(item.get("thinking") or "")
            if item.get("done"):
                final = item
        self.stream("/generate", body, collect, stop)
        if not final:
            raise RuntimeError("Ollama stream ended without a final result")
        return {"response": "".join(responses), "thinking": "".join(thoughts),
                "metrics": {"totalNs": final.get("total_duration") or 0,
                            "loadNs": final.get("load_duration") or 0,
                            "promptEvalCount": final.get("prompt_eval_count") or 0,
                            "promptEvalNs": final.get("prompt_eval_duration") or 0,
                            "evalCount": final.get("eval_count") or 0,
                            "evalNs": final.get("eval_duration") or 0,
                            "doneReason": final.get("done_reason") or "unknown"}}

    def pull(self, model, progress, stop=None):
        return self.stream("/pull", {"model": model, "stream": True}, progress, stop)
