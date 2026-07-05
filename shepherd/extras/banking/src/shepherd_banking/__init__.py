"""Shepherd Banking - Banking domain package for the Shepherd framework.

This package provides banking-specific functionality for AI agents:

- BankingContext: Financial operations context with transfer controls
- Tasks: Pre-built tasks for transfers and balance queries
- Effects: Domain-specific effects for audit trails

Quick Start
-----------
    import asyncio

    from shepherd import handle, workspace
    from shepherd_core.schema import SINGLE_OUTPUT_KEY
    from shepherd_runtime.provider_boundary import ModelResponse
    from shepherd_banking import BankingContext, query_balance

    async def main() -> None:
        def fake_model(request):
            return ModelResponse(
                structured_output={
                    SINGLE_OUTPUT_KEY: {
                        "balance": 1250.0,
                        "currency": "USD",
                        "account_name": "Operations Account",
                    }
                }
            )

        with workspace(model="offline-banking") as ws, handle("model.call", fake_model):
            ws.scope.bind(BankingContext(account_id="ACC-001"))
            result = await query_balance(account_id="ACC-001")
            print(result.balance)

    asyncio.run(main())

Effects
-------
Banking operations emit domain-specific effects:

- TransferInitiated: When a transfer is requested
- TransferCompleted: When a transfer succeeds
- TransferFailed: When a transfer fails
- BalanceQueried: When a balance is checked

    from shepherd_banking import TransferInitiated

    for effect in scope.effects.query(TransferInitiated):
        print(f"Transfer: {effect.amount} to {effect.to_account}")
"""

from __future__ import annotations

from shepherd_core.package import package

__version__ = "0.2.0"

# Context
from shepherd_banking.contexts import BankingContext

# Effects
from shepherd_banking.contexts.effects import (
    BalanceQueried,
    TransferCompleted,
    TransferFailed,
    TransferInitiated,
)

# Tasks (function-form per CONTRACTS A4 / Tranche 7)
from shepherd_banking.tasks import (
    BalanceResult,
    TransferResult,
    query_balance,
    transfer_funds,
)


@package(
    name="banking",
    version=__version__,
    tasks=["shepherd_banking.tasks"],
    contexts=["shepherd_banking.contexts"],
    effects=["shepherd_banking.contexts.effects"],
)
def banking() -> None:
    """Transfer funds, query balances, and manage accounts."""


__all__ = [
    "BalanceQueried",
    "BalanceResult",
    # Context
    "BankingContext",
    "TransferCompleted",
    "TransferFailed",
    # Effects
    "TransferInitiated",
    "TransferResult",
    # Version
    "__version__",
    # Package
    "banking",
    # Tasks
    "query_balance",
    "transfer_funds",
]
