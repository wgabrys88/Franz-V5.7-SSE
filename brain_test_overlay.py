import json
import random
import sys
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class _TestConfig:
    panel_url: str = "http://127.0.0.1:1236/v1/chat/completions"
    agent_name: str = "test_overlay"
    model: str = "qwen3.5-vl"
    cross_arm_length: int = 40
    stroke_width: int = 3
    stroke_color: str = "rgba(255,255,0,0.9)"


CFG: _TestConfig = _TestConfig()


def _make_cross_overlay(cx: int, cy: int) -> dict:
    arm: int = CFG.cross_arm_length
    return {
        "type": "overlay",
        "points": [
            [cx - arm, cy], [cx + arm, cy],
        ],
        "closed": False,
        "stroke": CFG.stroke_color,
        "stroke_width": CFG.stroke_width,
    }


def _make_cross_overlay_vertical(cx: int, cy: int) -> dict:
    arm: int = CFG.cross_arm_length
    return {
        "type": "overlay",
        "points": [
            [cx, cy - arm], [cx, cy + arm],
        ],
        "closed": False,
        "stroke": CFG.stroke_color,
        "stroke_width": CFG.stroke_width,
    }


def main() -> None:
    cx: int = random.randint(200, 800)
    cy: int = random.randint(200, 800)

    sys.stdout.write(f"placed cross at: {cx},{cy}\n")
    sys.stdout.flush()

    request_body: dict = {
        "model": CFG.model,
        "agent": CFG.agent_name,
        "capture_size": [640, 640],
        "messages": [
            {
                "role": "system",
                "content": (
                    """You see a screenshot with a yellow cross overlay drawn on it. """
                    """Return the center coordinates of the yellow cross. """
                    """Coordinates are normalized 0-1000. """
                    """Respond with ONLY valid JSON: {"x": <number>, "y": <number>}"""
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": ""},
                    },
                    {
                        "type": "actions",
                        "actions": [
                            _make_cross_overlay(cx, cy),
                            _make_cross_overlay_vertical(cx, cy),
                        ],
                    },
                    {
                        "type": "text",
                        "text": "Where is the yellow cross? Return JSON coordinates.",
                    },
                ],
            },
        ],
    }

    req: urllib.request.Request = urllib.request.Request(
        CFG.panel_url,
        data=json.dumps(request_body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_body: dict = json.loads(resp.read())
    except Exception as exc:
        sys.stderr.write(f"request failed: {exc}\n")
        sys.stderr.flush()
        raise SystemExit(1)

    choices: list = resp_body.get("choices", [])
    vlm_text: str = ""
    if choices and isinstance(choices[0], dict):
        vlm_text = choices[0].get("message", {}).get("content", "")

    sys.stdout.write(f"vlm responded: {vlm_text}\n")
    sys.stdout.write(f"expected: x={cx}, y={cy}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
