"""Causal, conserving creator-fee accrual from retained Pump TradeEvents.

Accrual is a receivable estimate, not evidence of a vault withdrawal. Shared
allocations use the shares evidenced at each event; floor-rounding dust remains
unallocated rather than inventing a recipient for it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CreatorFeeAllocation:
    fee_lamports: int
    credits: tuple[tuple[str, int], ...]
    unallocated_lamports: int
    reason: str


def allocate_creator_fee(row: dict[str, Any]) -> CreatorFeeAllocation:
    fee = int(row["creator_fee"])
    if fee < 0:
        raise ValueError("negative creator fee")
    if not fee:
        return CreatorFeeAllocation(0, (), 0, "ZERO_FEE")
    payload = row["payload"]
    shares = payload.get("shareholders")
    if shares:
        try:
            recipients = [(s["address"], s["share_bps"]) for s in shares]
            if (
                len(recipients) > 10
                or len({a for a, _ in recipients}) != len(recipients)
                or any(
                    not isinstance(a, str)
                    or not a
                    or a == "11111111111111111111111111111111"
                    or type(bps) is not int
                    or not 0 < bps <= 10_000
                    for a, bps in recipients
                )
                or sum(bps for _, bps in recipients) != 10_000
            ):
                raise ValueError("invalid shares")
        except (KeyError, TypeError, ValueError):
            return CreatorFeeAllocation(fee, (), fee, "UNPROVEN_SHARED_RECIPIENTS")
        credits = tuple((a, fee * bps // 10_000) for a, bps in recipients)
        residual = fee - sum(v for _, v in credits)
        return CreatorFeeAllocation(fee, credits, residual, "EVENT_SHARE_ACCRUAL")
    recipient = payload.get("creator")
    if (
        not isinstance(recipient, str)
        or not recipient
        or recipient == "11111111111111111111111111111111"
    ):
        return CreatorFeeAllocation(fee, (), fee, "UNPROVEN_SINGLE_RECIPIENT")
    return CreatorFeeAllocation(fee, ((recipient, fee),), 0, "EVENT_CREATOR_VAULT_ACCRUAL")


class CreatorFeeIncome:
    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.by_wallet: defaultdict[tuple[str, str], int] = defaultdict(int)
        self.total: defaultdict[str, int] = defaultdict(int)
        self.unallocated: defaultdict[str, int] = defaultdict(int)

    def observe(self, event_id: str, row: dict[str, Any]) -> None:
        if event_id in self.seen:
            return
        allocation = allocate_creator_fee(row)
        self.seen.add(event_id)
        mint = row["mint"]
        self.total[mint] += allocation.fee_lamports
        self.unallocated[mint] += allocation.unallocated_lamports
        for wallet, amount in allocation.credits:
            self.by_wallet[mint, wallet] += amount

    def income(self, mint: str, wallet: str) -> int:
        return self.by_wallet.get((mint, wallet), 0)

    def verify(self) -> None:
        credited: defaultdict[str, int] = defaultdict(int)
        for (mint, _), amount in self.by_wallet.items():
            credited[mint] += amount
        if any(credited[m] + self.unallocated[m] != total for m, total in self.total.items()):
            raise ValueError("creator fee allocation does not conserve accrued fees")
