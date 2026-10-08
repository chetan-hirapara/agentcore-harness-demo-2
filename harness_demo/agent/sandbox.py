"""Put OUR calculator on the sandbox before the agent reasons.

The policy engine is shipped, not model-generated: a sandbox does not
make arithmetic deterministic if the model writes the arithmetic.

Harness sessions only expose the shell WebSocket (InvokeAgentRuntimeCommand
rejects harness ARNs with a 404), so the file travels as base64 lines
short enough for the PTY's line buffer, over a SigV4-signed WebSocket.
"""
import asyncio
import base64
import json
import logging
import urllib.parse
from collections.abc import Callable
from pathlib import Path

import botocore.session
import websockets
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from harness_demo.config import CALC_REMOTE_PATH, Settings
from harness_demo.errors import SandboxSeedError
from harness_demo.policy import refund_calc

log = logging.getLogger(__name__)

CALC_LOCAL_PATH = Path(refund_calc.__file__)
SEED_TIMEOUT_S = 360
_FRAME_BYTES = 16 * 1024            # the service caps frames at 64 KB


def build_seed_script(calc_path: Path = CALC_LOCAL_PATH,
                      remote_path: str = CALC_REMOTE_PATH) -> str:
    b64 = base64.b64encode(calc_path.read_bytes()).decode()
    lines = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))
    return (f"stty -echo; base64 -d > {remote_path} <<'B64'\n"
            f"{lines}\nB64\ntest -s {remote_path}\nexit\n")


class CalculatorSandbox:
    def __init__(self, settings: Settings, harness_arn: Callable[[], str]):
        self.settings = settings
        self._harness_arn = harness_arn

    def seed(self, session_id: str) -> None:
        self._shell_exec(session_id, build_seed_script())
        log.info("calculator shipped to sandbox session %s", session_id[:8])

    def _shell_exec(self, session_id: str, script: str) -> None:
        """Run a bash script in the session's microVM, no model involved."""
        try:
            asyncio.run(asyncio.wait_for(
                self._shell_exec_async(session_id, script), SEED_TIMEOUT_S))
        except (OSError, TimeoutError, websockets.WebSocketException,
                RuntimeError) as e:
            raise SandboxSeedError(
                f"could not seed the calculator: {type(e).__name__}: {e}"
            ) from e

    async def _shell_exec_async(self, session_id: str, script: str) -> None:
        arn, region = self._harness_arn(), self.settings.region
        host = f"bedrock-agentcore.{region}.amazonaws.com"
        path = f"/runtimes/{urllib.parse.quote(arn, safe='')}/ws/shells"
        request = AWSRequest(method="GET", url=f"https://{host}{path}", headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id})
        SigV4Auth(botocore.session.Session().get_credentials(),
                  "bedrock-agentcore", region).add_auth(request)

        data = script.encode()
        async with websockets.connect(
                f"wss://{host}{path}",
                additional_headers=dict(request.headers),
                subprotocols=["v1.command.agentcore.aws.dev"],
                open_timeout=330) as ws:
            for i in range(0, len(data), _FRAME_BYTES):
                await ws.send(b"\x00" + data[i:i + _FRAME_BYTES])
            async for frame in ws:
                if isinstance(frame, bytes) and frame[:1] == b"\x03":
                    status = json.loads(frame[1:])
                    for cause in status.get("details", {}).get("causes", []):
                        if cause.get("reason") == "ExitCode":
                            raise RuntimeError(f"seeding failed: {status}")
