"""Configuration: one frozen object, no work done at import time.

The old client resolved the harness ARN by shelling out to the AWS CLI
while the module was being imported, so merely importing it needed
credentials and a network. Here nothing runs until `resolve_harness_arn`
is called.
"""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import boto3
from botocore.config import Config

from harness_demo.errors import ConfigError

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CALC_REMOTE_PATH = "/tmp/refund_calc.py"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


@dataclass(frozen=True)
class Settings:
    region: str = field(default_factory=lambda: _env("AWS_REGION", "us-east-1"))
    harness_name: str = field(
        default_factory=lambda: _env("HARNESS_NAME", "support_agent"))
    # Explicit ARN wins; otherwise it is looked up by harness_name.
    harness_arn: str = field(default_factory=lambda: _env("HARNESS_ARN", ""))
    db_path: str = field(default_factory=lambda: _env(
        "DEMO_DB_PATH", str(PROJECT_ROOT / "demo_orders.db")))
    episodes_dir: str = field(default_factory=lambda: _env(
        "EPISODES_DIR", str(PROJECT_ROOT / "episodes")))

    def boto_config(self) -> Config:
        # invoke_harness streams for minutes, so the read timeout is long.
        return Config(retries={"max_attempts": 5, "mode": "standard"},
                      connect_timeout=10, read_timeout=300)

    def client(self, service: str):
        return boto3.client(service, region_name=self.region,
                            config=self.boto_config())


def resolve_harness_arn(settings: Settings, control=None) -> str:
    """The explicit ARN if set, else the harness named `settings.harness_name`."""
    if settings.harness_arn:
        return settings.harness_arn
    control = control or settings.client("bedrock-agentcore-control")
    kwargs: dict = {}
    while True:
        page = control.list_harnesses(**kwargs)
        for h in page.get("harnesses", []):
            if h.get("harnessName") == settings.harness_name:
                log.info("resolved harness %s", settings.harness_name)
                return h["arn"]
        token = page.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    raise ConfigError(
        f"No harness named {settings.harness_name!r} in {settings.region}. "
        "Run setup.ps1 / setup.sh, or set HARNESS_ARN.")
