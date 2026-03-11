import json
import re
import threading
import time
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class _Config:
    panel_url: str = "http://127.0.0.1:1236/v1/chat/completions"
    model: str = "qwen3.5-vl"
    agent: str = "chess_executor"
    region: str = "92,271,266,581"
    grid_size: int = 8
    norm: int = 1000


CFG: _Config = _Config()
_busy: threading.Lock = threading.Lock()


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
                            if current_event == "routed_response":
                                try:
                                    data: dict = json.loads(line[6:])
                                    text: str = data.get("text", "")
                                    if text:
                                        self.callback(text)
                                except Exception:
                                    pass
                            current_event = ""
            except Exception:
                time.sleep(1)


def _grid_to_norm(col: int, row: int) -> tuple[int, int]:
    step: int = CFG.norm // CFG.grid_size
    return col * step + step // 2, row * step + step // 2


def _extract_json(text: str) -> str:
    t: str = text.strip()
    m: re.Match[str] | None = re.search(r"```\w*\s*\n?(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    m2: re.Match[str] | None = re.search(r"\{[^{}]*\}", t)
    if m2:
        return m2.group(0)
    return t


def execute_move(text: str) -> None:
    if not _busy.acquire(blocking=False):
        print("Executor busy, skipping move")
        return
    try:
        move: dict = json.loads(_extract_json(text))
        print(f"Raw VLM move: {move}")
        from_x, from_y = _grid_to_norm(int(move["from_x"]), int(move["from_y"]))
        to_x, to_y = _grid_to_norm(int(move["to_x"]), int(move["to_y"]))
        print(f"Normalized: ({from_x},{from_y}) -> ({to_x},{to_y})")
        body: dict = {
            "model": CFG.model,
            "agent": CFG.agent,
            "region": CFG.region,
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "actions",
                    "actions": [{
                        "type": "drag",
                        "x1": from_x, "y1": from_y,
                        "x2": to_x, "y2": to_y,
                    }],
                }],
            }],
        }
        req: urllib.request.Request = urllib.request.Request(
            CFG.panel_url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except Exception as e:
        print(f"Executor error: {e}")
    finally:
        _busy.release()


def main() -> None:
    print("Chess Executor started")
    SSEListener(CFG.agent, execute_move)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
