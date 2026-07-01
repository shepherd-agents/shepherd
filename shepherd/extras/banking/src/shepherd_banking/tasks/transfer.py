"""TransferFunds task for initiating bank transfers.

Function-form (DECISIONS D5 / Tranche 7): the previous class-form
``@task class TransferFunds(BaseModel)`` is replaced with the
function-form ``@task async def transfer_funds(...) -> TransferResult``
shape per CONTRACTS A4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from annotated_types import Gt
from shepherd_runtime.nucleus import deliver, task
from shepherd_runtime.scope import current_binding
from shepherd_runtime.task.markers import InputMarker

from ..contexts import BankingContext

_GUIDANCE = """\
Transfer funds between accounts using the active BankingContext.

Verify the context allows transfers (allow_transfers=True). Verify
the amount is within the context's transfer_limit. Initiate the
transfer and return its outcome:

- status="completed" plus a transaction_id on success.
- status="failed" plus an error_message describing why on failure.
- status="pending" if the transfer is queued for asynchronous
  settlement.
"""


@dataclass(frozen=True)
class TransferResult:
    """Result of a fund transfer attempt."""

    status: str = ""
    transaction_id: str = ""
    error_message: str = ""


@task(guidance=_GUIDANCE)
async def transfer_funds(
    to_account: Annotated[str, InputMarker(description="Destination account ID")],
    amount: Annotated[
        float, InputMarker(description="Amount to transfer"), Gt(0)
    ],
    reference: Annotated[
        str, InputMarker(description="Transfer reference/memo")
    ] = "",
) -> TransferResult:
    """Transfer funds between accounts.

    The active BankingContext (looked up by type via
    ``current_binding(BankingContext)``) must have
    ``allow_transfers=True`` and the amount must be within its
    ``transfer_limit``.

    Example::

        from shepherd import workspace
        from shepherd_banking import BankingContext, transfer_funds

        ws = workspace(model=...)
        with ws.scope:
            ws.scope.bind(BankingContext(
                account_id="ACC-001",
                allow_transfers=True,
                transfer_limit=10_000.0,
            ))
            result = await transfer_funds(
                to_account="ACC-002",
                amount=500.0,
                reference="Invoice #123",
            )
            print(f"Status: {result.status}")
    """
    banking = current_binding(BankingContext)
    return await deliver(
        TransferResult,
        goal=(
            "Initiate the transfer described in evidence and return "
            "its outcome."
        ),
        evidence=[
            f"source_account={banking.account_id}",
            f"to_account={to_account}",
            f"amount={amount}",
            f"reference={reference}",
            f"allow_transfers={banking.allow_transfers}",
            f"transfer_limit={banking.transfer_limit}",
        ],
    )


__all__ = ["TransferResult", "transfer_funds"]
