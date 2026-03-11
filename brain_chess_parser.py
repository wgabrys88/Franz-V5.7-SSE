import json
import threading
import time
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class _Config:
    panel_url: str = "http://127.0.0.1:1236/v1/chat/completions"
    model: str = "qwen3.5-vl"
    agent: str = "chess_parser"
    region: str = "92,271,266,581"
    temperature: float = 0.3
    max_tokens: int = 60
    grid_size: int = 8
    norm: int = 1000
    grid_color: str = "rgba(0,255,200,0.95)"
    grid_stroke_width: int = 4


CFG: _Config = _Config()
_busy: threading.Lock = threading.Lock()

SYSTEM_PROMPT: str = """You output raw JSON only. No markdown. No backticks. No explanation."""

USER_PROMPT: str = (
    """Grid has 8 columns (0=left to 7=right) and 8 rows (0=top to 7=bottom). """
    """What is the best move? Reply ONLY: {"from_x":C,"from_y":R,"to_x":C,"to_y":R}"""
)


def _make_grid_overlays() -> list[dict]:
    overlays: list[dict] = []
    step: int = CFG.norm // CFG.grid_size
    for i in range(CFG.grid_size + 1):
        pos: int = i * step
        overlays.append({
            "type": "overlay",
            "points": [[pos, 0], [pos, CFG.norm]],
            "closed": False,
            "stroke": CFG.grid_color,
            "stroke_width": CFG.grid_stroke_width,
        })
        overlays.append({
            "type": "overlay",
            "points": [[0, pos], [CFG.norm, pos]],
            "closed": False,
            "stroke": CFG.grid_color,
            "stroke_width": CFG.grid_stroke_width,
        })
    return overlays


class SSEListener:
    def __init__(self, agent: str, callback: object) -> None:
        self.url: str = f"http://127.0.0.1:1236/agent-events?agent={agent}"
        self.callback: object = callback
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self) -> None:
        while True:
            try:
                with urllib.request.urlopen(self.url, timeout=6000) as resp:
                    current_event: str = ""
                    for raw_line in resp:
                        line: str = raw_line.decode().rstrip("\r\n")
                        if line.startswith("event: "):
                            current_event = line[7:]
                        elif line.startswith("data: "):
                            if current_event in ("routed_request", "routed_response"):
                                try:
                                    self.callback(current_event, json.loads(line[6:]))
                                except Exception:
                                    pass
                            current_event = ""
            except Exception:
                time.sleep(1)


def handle_routed(event: str, data: dict) -> None:
    if event != "routed_request":
        return
    if not _busy.acquire(blocking=False):
        print("Parser busy, skipping")
        return
    try:
        print("Parser received board, adding cyan grid")
        body: dict = {
            "model": CFG.model,
            "agent": CFG.agent,
            "region": CFG.region,
            "capture_size": [640, 640],
            "recipients": ["chess_executor"],
            "temperature": CFG.temperature,
            "max_tokens": CFG.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": ""}},
                    {"type": "actions", "actions": _make_grid_overlays()},
                    {"type": "text", "text": USER_PROMPT},
                ]},
            ],
        }
        req: urllib.request.Request = urllib.request.Request(
            CFG.panel_url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except Exception as e:
        print(f"Parser error: {e}")
    finally:
        _busy.release()


def main() -> None:
    print("Chess Parser started")
    SSEListener(CFG.agent, handle_routed)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
