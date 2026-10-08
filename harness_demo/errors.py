"""Exceptions shared across the package."""


class HarnessError(Exception):
    """Base class for every error this package raises on purpose."""


class ConfigError(HarnessError):
    """Required configuration (for example the harness ARN) is missing."""


class SandboxSeedError(HarnessError):
    """The policy calculator could not be placed on the sandbox microVM."""


class EpisodeAborted(HarnessError):
    """An episode stopped on a budget or harness limit.

    This is a bounded, traced failure: the caller gets a machine-readable
    `reason` instead of an unbounded run.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)
