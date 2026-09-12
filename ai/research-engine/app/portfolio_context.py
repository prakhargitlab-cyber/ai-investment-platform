"""Future privacy boundary for short-lived customer portfolio context.

No implementation is selected in this iteration.  Public company research uses
the canonical global instrument IDs projected from this context and remains
durable independently of customer quantities, costs, values, and P&L.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class PortfolioHoldingContext:
    global_instrument_id: UUID
    quantity: Decimal | None = None
    average_cost: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None


@dataclass(frozen=True)
class PortfolioContext:
    context_id: str
    owner_session_id: str
    portfolio_id: UUID
    holdings: tuple[PortfolioHoldingContext, ...]
    expires_at: datetime

    @property
    def global_instrument_ids(self) -> tuple[UUID, ...]:
        """Return a stable, deduplicated public-research projection."""
        return tuple(dict.fromkeys(item.global_instrument_id for item in self.holdings))


class PortfolioContextStore(Protocol):
    """Storage seam for Postgres DEV, memory tests, and Redis production later."""

    async def load(self, owner_session_id: str, portfolio_id: UUID) -> PortfolioContext | None:
        ...

    async def save(self, context: PortfolioContext) -> None:
        ...

    async def delete(self, owner_session_id: str, portfolio_id: UUID) -> None:
        ...
