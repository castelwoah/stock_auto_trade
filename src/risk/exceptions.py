from __future__ import annotations


class RiskBlocked(Exception):
    """Raised when a trade is blocked by the risk layer.

    `code` is a short machine-readable tag (e.g. "daily_loss_limit",
    "circuit_breaker", "blacklist") that downstream logging / RiskEvent
    persistence can index on. `detail` is a human-readable reason.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"[{code}] {detail}")
        self.code = code
        self.detail = detail

    def __repr__(self) -> str:
        return f"RiskBlocked(code={self.code!r}, detail={self.detail!r})"
