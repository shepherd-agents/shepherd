"""One supported configuration for the current checker."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CheckerConfig:
    """Frozen resource budgets for one Opus-only workflow."""

    batch_size: int = 6
    workers: int = 5
    provider_seconds: int = 300
    worker_seconds: int = 360
    searches: int = 6
    fetches: int = 12
    batch_input_tokens: int = 12000
    batch_output_tokens: int = 3200

    def __post_init__(self) -> Any:
        if not 1 <= self.batch_size <= 10 or not 1 <= self.workers <= 5:
            raise ValueError("Use 1-10 citations per batch and 1-5 workers")
        if self.provider_seconds <= 0 or self.worker_seconds <= self.provider_seconds:
            raise ValueError("Worker timeout must exceed the positive provider timeout")
        if min(self.searches, self.fetches) < 0 or min(self.batch_input_tokens, self.batch_output_tokens) <= 0:
            raise ValueError("Invalid search, fetch or batch budget")

    def to_dict(self) -> Any:
        return asdict(self)
