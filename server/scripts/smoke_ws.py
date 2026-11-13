"""E2E smoke test for the WebSocket observation stream.

Connects to the server's /ws/observations, prints frames as they
arrive, and validates the bus subscriber push (added in step 8).
Exits 0 once N frames have been seen within TIMEOUT.
"""
import asyncio
import json
import sys
import time

import websockets

URL = "ws://localhost:8000/ws/observations"
REQUIRED_FRAMES = 3
TIMEOUT_S = 8.0


async def main() -> int:
    deadline = time.monotonic() + TIMEOUT_S
    received: list[dict] = []
    try:
        async with websockets.connect(URL) as ws:
            print(f"connected to {URL}", flush=True)
            while time.monotonic() < deadline and len(received) < REQUIRED_FRAMES:
                remaining = deadline - time.monotonic()
                msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                try:
                    frame = json.loads(msg)
                except json.JSONDecodeError:
                    print(f"non-JSON frame: {msg!r}", flush=True)
                    continue
                if frame.get("type") == "obs":
                    received.append(frame)
                    print(
                        f"  [{len(received)}] {frame['data']['drone_id']} @ "
                        f"({frame['data']['position']['lat']:.4f}, "
                        f"{frame['data']['position']['lon']:.4f})",
                        flush=True,
                    )
    except (OSError, websockets.WebSocketException) as e:
        print(f"WS error: {e}", flush=True)
        return 2

    if len(received) < REQUIRED_FRAMES:
        print(
            f"FAIL: only got {len(received)} frames in {TIMEOUT_S}s "
            f"(needed {REQUIRED_FRAMES})",
            flush=True,
        )
        return 1

    print(f"OK: received {len(received)} obs frames", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
