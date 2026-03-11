import json
import time
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class _Config:
    panel_url: str = "http://127.0.0.1:1236/v1/chat/completions"
    model: str = "qwen3.5-vl"
    agent: str = "chess_capturer"
    region: str = "92,271,266,581"
    temperature: float = 0.1
    max_tokens: int = 3
    loop_delay: float = 6.0


CFG: _Config = _Config()

SYSTEM_PROMPT: str = """Say OK."""

USER_PROMPT: str = """OK?"""


def main() -> None:
    print("Chess Capturer started")
    while True:
        body: dict = {
            "model": CFG.model,
            "agent": CFG.agent,
            "region": CFG.region,
            "capture_size": [640, 640],
            "recipients": ["chess_parser"],
            "temperature": CFG.temperature,
            "max_tokens": CFG.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": ""}},
                    {"type": "text", "text": USER_PROMPT},
                ]},
            ],
        }
        try:
            req: urllib.request.Request = urllib.request.Request(
                CFG.panel_url,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp.read()
        except Exception as e:
            print(f"Capturer warning: {e}")
        time.sleep(CFG.loop_delay)


if __name__ == "__main__":
    main()
