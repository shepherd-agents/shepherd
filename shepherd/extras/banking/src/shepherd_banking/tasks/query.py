"""QueryBalance task for checking account balances.

Function-form (DECISIONS D5 / Tranche 7): the previous class-form
``@task class QueryBalance(BaseModel)`` is replaced with the
function-form ``@task async def query_balance(...) -> BalanceResult``
shape per CONTRACTS A4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from shepherd_runtime.nucleus import deliver, task
from shepherd_runtime.scope import current_binding
from shepherd_runtime.task.markers import InputMarker

from ..contexts import BankingContext

_GUIDANCE = """\
Query the balance of a banking account using the active
BankingContext. Works with both read-only and transfer-enabled
contexts.

Return:
- balance: current account balance
- currency: currency code (default USD)
- account_name: account name if available
"""


@dataclass(frozen=True)
class BalanceResult:
    """Result of a balance query."""

    balance: float = 0.0
    currency: str = "USD"
    account_name: str = ""


@task(guidance=_GUIDANCE)
async def query_balance(
    account_id: Annotated[
        str,
        InputMarker(
            description=(
                "Account ID to query (defaults to the context's account)"
            )
        ),
    ] = "",
) -> BalanceResult:
    """Query the balance of a banking account.

    The active BankingContext (looked up by type via
    ``current_binding(BankingContext)``) provides the default account
    when ``account_id`` is empty.

    Example::

        from shepherd import workspace
        from shepherd_banking import BankingContext, query_balance

        ws = workspace(model=...)
        with ws.scope:
            ws.scope.bind(BankingContext(
                account_id="ACC-001",
                account_name="Operations",
            ))
            result = await query_balance(account_id="ACC-001")
            print(f"Balance: {result.balance} {result.currency}")
    """
    banking = current_binding(BankingContext)
    target_account = account_id or banking.account_id
    return await deliver(
        BalanceResult,
        goal="Return the current balance for the target account.",
        evidence=[
            f"target_account={target_account}",
            f"context_account={banking.account_id}",
            f"context_account_name={banking.account_name}",
        ],
    )


__all__ = ["BalanceResult", "query_balance"]
