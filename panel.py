import base64
import http.server
import json
import logging
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class _Config:
    host: str = "127.0.0.1"
    port: int = 1236
    vlm_url: str = "http://127.0.0.1:1235/v1/chat/completions"
    annotate_timeout: float = 3.0


CFG: _Config = _Config()
WIN32_PATH: Path = Path(__file__).resolve().parent / "win32.py"
PANEL_HTML: Path = Path(__file__).resolve().parent / "panel.html"
HERE: Path = Path(__file__).resolve().parent


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(record.msg) if isinstance(record.msg, dict) else super().format(record)


_log_handler: logging.FileHandler = logging.FileHandler(HERE / "franz-log.jsonl", encoding="utf-8")
_log_handler.setFormatter(_JsonFormatter())
_logger: logging.Logger = logging.getLogger("franz")
_logger.setLevel(logging.DEBUG)
_logger.addHandler(_log_handler)

_pending: dict[str, dict[str, Any]] = {}
_pending_lock: threading.Lock = threading.Lock()

_sse_lock: threading.Lock = threading.Lock()
_sse_queues: list[Any] = []

_agent_sse_lock: threading.Lock = threading.Lock()
_agent_sse_queues: dict[str, list[Any]] = {}


def _sse_push(event: str, data: dict[str, Any]) -> None:
    import queue as _q
    chunk: bytes = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
    with _sse_lock:
        dead: list[Any] = []
        for q in _sse_queues:
            try:
                q.put_nowait(chunk)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)


def _agent_sse_push(agent: str, event: str, data: dict[str, Any]) -> None:
    import queue as _q
    chunk: bytes = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
    with _agent_sse_lock:
        queues: list[Any] = _agent_sse_queues.get(agent, [])
        dead: list[Any] = []
        for q in queues:
            try:
                q.put_nowait(chunk)
            except Exception:
                dead.append(q)
        for q in dead:
            queues.remove(q)


def _capture(region: str, w: int = 640, h: int = 640) -> str:
    cmd: list[str] = [sys.executable, str(WIN32_PATH), "capture", "--width", str(w), "--height", str(h)]
    if region:
        cmd.extend(["--region", region])
    proc: subprocess.CompletedProcess[bytes] = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return ""
    return base64.b64encode(proc.stdout).decode("ascii")


def _win32(args: list[str], region: str) -> None:
    cmd: list[str] = [sys.executable, str(WIN32_PATH)] + args
    if region and "--region" not in args:
        cmd.extend(["--region", region])
    subprocess.run(cmd, capture_output=True)


def _dispatch_physical(act: dict[str, Any], region: str) -> None:
    t: str = act.get("type", "")
    _logger.debug({"event": "action_dispatched", "ts": time.time(), **{k: v for k, v in act.items()}})
    match t:
        case "drag":
            _win32(["drag",
                    "--from_pos", f"{act['x1']},{act['y1']}",
                    "--to_pos",   f"{act['x2']},{act['y2']}"], region)
        case "click":
            _win32(["click", "--pos", f"{act['x']},{act['y']}"], region)
        case "double_click":
            _win32(["double_click", "--pos", f"{act['x']},{act['y']}"], region)
        case "right_click":
            _win32(["right_click", "--pos", f"{act['x']},{act['y']}"], region)
        case "type_text":
            _win32(["type_text", "--text", act.get("text", "")], region)
        case "press_key":
            _win32(["press_key", "--key", act.get("key", "")], region)
        case "hotkey":
            _win32(["hotkey", "--keys", act.get("keys", "")], region)
        case "scroll_up":
            _win32(["scroll_up", "--pos", f"{act['x']},{act['y']}",
                    "--clicks", str(act.get("clicks", 3))], region)
        case "scroll_down":
            _win32(["scroll_down", "--pos", f"{act['x']},{act['y']}",
                    "--clicks", str(act.get("clicks", 3))], region)
        case "cursor_pos":
            _win32(["cursor_pos"], region)


def _process_content_parts(
    messages: list[Any], region: str, capture_size: list[int]
) -> tuple[list[dict[str, Any]], str, str]:
    overlays: list[dict[str, Any]] = []
    raw_b64: str = ""
    request_text: str = ""
    for msg in messages:
        content: Any = msg.get("content", "")
        if isinstance(content, str):
            if msg.get("role") in ("user", "system") and not request_text:
                request_text = content
            continue
        if not isinstance(content, list):
            continue
        stripped: list[dict[str, Any]] = []
        for part in content:
            if part.get("type") == "image_url":
                url: str = part.get("image_url", {}).get("url", "")
                if url == "":
                    raw_b64 = _capture(region, capture_size[0], capture_size[1])
                    part["image_url"]["url"] = f"data:image/png;base64,{raw_b64}"
                else:
                    raw_b64 = url.split(",", 1)[1]
                stripped.append(part)
            elif part.get("type") == "actions":
                for act in part.get("actions", []):
                    if act.get("type") == "overlay":
                        overlays.append(act)
                    else:
                        _dispatch_physical(act, region)
            else:
                stripped.append(part)
                if part.get("type") == "text" and not request_text:
                    request_text = part.get("text", "")
        msg["content"] = stripped
    return overlays, raw_b64, request_text


def _process_response_actions(
    resp_obj: dict[str, Any], region: str
) -> tuple[list[dict[str, Any]], str]:
    response_overlays: list[dict[str, Any]] = []
    text: str = ""
    choices: list[Any] = resp_obj.get("choices", [])
    if not choices or not isinstance(choices[0], dict):
        return response_overlays, text
    message: dict[str, Any] = choices[0].get("message", {})
    text = message.get("content", "")
    content: Any = message.get("content", "")
    if not isinstance(content, list):
        return response_overlays, text
    stripped: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for part in content:
        if part.get("type") == "actions":
            for act in part.get("actions", []):
                if act.get("type") == "overlay":
                    response_overlays.append(act)
                else:
                    _dispatch_physical(act, region)
        else:
            stripped.append(part)
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
    message["content"] = stripped if stripped else (text_parts[0] if text_parts else "")
    text = text_parts[0] if text_parts else (text if isinstance(text, str) else "")
    return response_overlays, text


def _annotate_via_chrome(
    rid: str, raw_b64: str, overlays: list[dict[str, Any]],
    model: str, agent: str, request_text: str
) -> str | None:
    slot_ref: dict[str, Any] = {"event": threading.Event(), "result": ""}
    with _pending_lock:
        _pending[rid] = slot_ref
    _sse_push("annotate", {
        "request_id": rid,
        "raw_b64": raw_b64,
        "overlays": overlays,
        "model": model,
        "agent": agent,
        "request_text": request_text,
    })
    got_result: bool = slot_ref["event"].wait(timeout=CFG.annotate_timeout)
    with _pending_lock:
        _pending.pop(rid, None)
    if not got_result:
        _logger.debug({"event": "annotate_timeout", "ts": time.time(), "agent": agent, "request_id": rid})
        return None
    return slot_ref["result"]


class PanelHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")

    def _json(self, code: int, data: dict[str, Any]) -> None:
        body: bytes = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path: str = self.path.split("?")[0]
        if path == "/":
            body: bytes = PANEL_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        elif path == "/ready":
            self._json(200, {"ok": True})
        elif path == "/events":
            import queue as _q
            q: _q.Queue[bytes | None] = _q.Queue()
            with _sse_lock:
                _sse_queues.append(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(b"event: connected\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        chunk: bytes | None = q.get(timeout=25)
                    except Exception:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if chunk is None:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                with _sse_lock:
                    try:
                        _sse_queues.remove(q)
                    except ValueError:
                        pass
        elif path == "/agent-events":
            import queue as _q
            from urllib.parse import parse_qs, urlparse
            params: dict[str, list[str]] = parse_qs(urlparse(self.path).query)
            agent_name: str = params.get("agent", [""])[0]
            if not agent_name:
                self._json(400, {"error": "agent parameter required"})
                return
            q_agent: _q.Queue[bytes | None] = _q.Queue()
            with _agent_sse_lock:
                if agent_name not in _agent_sse_queues:
                    _agent_sse_queues[agent_name] = []
                _agent_sse_queues[agent_name].append(q_agent)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(b"event: connected\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        chunk = q_agent.get(timeout=25)
                    except Exception:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if chunk is None:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                with _agent_sse_lock:
                    agent_list: list[Any] = _agent_sse_queues.get(agent_name, [])
                    try:
                        agent_list.remove(q_agent)
                    except ValueError:
                        pass
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path: str = self.path.split("?")[0]
        length: int = int(self.headers.get("Content-Length", 0))
        body: bytes = self.rfile.read(length) if length else b""

        if path == "/v1/chat/completions":
            try:
                req_body: dict[str, Any] = json.loads(body)
            except Exception:
                self._json(400, {"error": "bad json"})
                return

            region: str = req_body.pop("region", "")
            agent: str = req_body.pop("agent", "default")
            capture_size: list[int] = req_body.pop("capture_size", [640, 640])
            recipients: list[str] = req_body.pop("recipients", [])

            messages: list[Any] = req_body.get("messages", [])

            overlays, raw_b64, request_text = _process_content_parts(messages, region, capture_size)

            rid: str = str(uuid.uuid4())
            t_req: float = time.time()
            _logger.debug({"event": "vlm_request", "ts": t_req, "model": req_body.get("model", ""), "agent": agent, "overlays": len(overlays), "request_id": rid})

            annotated_b64: str | None = _annotate_via_chrome(rid, raw_b64, overlays, req_body.get("model", ""), agent, request_text)

            if annotated_b64 is None:
                self._json(504, {"error": "annotate timeout"})
                return

            if annotated_b64 and raw_b64:
                for msg in messages:
                    content: Any = msg.get("content", "")
                    if not isinstance(content, list):
                        continue
                    for part in content:
                        if part.get("type") == "image_url":
                            part["image_url"]["url"] = f"data:image/png;base64,{annotated_b64}"

            for recipient in recipients:
                _agent_sse_push(recipient, "routed_request", {
                    "from_agent": agent,
                    "request_id": rid,
                    "request_text": request_text,
                    "model": req_body.get("model", ""),
                })

            fwd_body: bytes = json.dumps(req_body).encode()
            fwd_req: urllib.request.Request = urllib.request.Request(
                CFG.vlm_url, data=fwd_body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(fwd_req, timeout=360) as resp:
                    resp_bytes: bytes = resp.read()
                resp_obj: dict[str, Any] = json.loads(resp_bytes)

                response_overlays, text = _process_response_actions(resp_obj, region)

                if response_overlays and annotated_b64:
                    resp_rid: str = str(uuid.uuid4())
                    resp_annotated: str | None = _annotate_via_chrome(
                        resp_rid, annotated_b64, response_overlays,
                        req_body.get("model", ""), agent, ""
                    )
                    if resp_annotated:
                        annotated_b64 = resp_annotated

                _logger.debug({"event": "vlm_response", "ts": time.time(), "duration_ms": round((time.time() - t_req) * 1000), "text": text, "agent": agent, "request_id": rid})
                _sse_push("vlm_done", {"request_id": rid, "text": text, "annotated_b64": annotated_b64 or "", "agent": agent, "model": req_body.get("model", "")})

                for recipient in recipients:
                    _agent_sse_push(recipient, "routed_response", {
                        "from_agent": agent,
                        "request_id": rid,
                        "text": text,
                        "model": req_body.get("model", ""),
                    })

                resp_bytes = json.dumps(resp_obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self._cors()
                self.end_headers()
                self.wfile.write(resp_bytes)
            except Exception as exc:
                _logger.debug({"event": "vlm_error", "ts": time.time(), "error": str(exc), "agent": agent, "request_id": rid})
                _sse_push("vlm_done", {"request_id": rid, "text": f"ERROR: {exc}", "annotated_b64": annotated_b64 or "", "agent": agent, "model": req_body.get("model", "")})
                self._json(502, {"error": str(exc)})

        elif path == "/result":
            try:
                data: dict[str, Any] = json.loads(body)
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            rid_val: str = data.get("request_id", "")
            annotated: str = data.get("annotated_b64", "")
            with _pending_lock:
                slot: dict[str, Any] | None = _pending.pop(rid_val, None)
            if slot:
                slot["result"] = annotated
                slot["event"].set()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "unknown request_id"})

        elif path == "/panel-log":
            try:
                data = json.loads(body)
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            _logger.debug({"event": "panel_js", "ts": time.time(), **data})
            self._json(200, {"ok": True})

        else:
            self._json(404, {"error": "not found"})


def start(host: str = CFG.host, port: int = CFG.port) -> http.server.ThreadingHTTPServer:
    server: http.server.ThreadingHTTPServer = http.server.ThreadingHTTPServer((host, port), PanelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    start().serve_forever()
