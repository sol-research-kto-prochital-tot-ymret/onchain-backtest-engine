"""Causal multi-mint Pump execution, isolated from the legacy SOL-only runtime.

Historical AFTER observations replace recorded state; simulated fills update the
selected venues atomically. Later recorded observations are exogenous, so this
is conditional recorded-path replay, never a market-impact counterfactual.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from backtest.plugins.protocols.pumpfun.quotes import (
    SOL_MINT,
    QuoteError,
    QuoteState,
    RouteResult,
    route_quote,
    sweep_fees,
)

StateTiming = Literal["after_transaction", "after_outer_instruction"]
ExecutionStatus = Literal["filled", "slippage_failed", "rejected", "unresolved"]


class ReplayError(ValueError):
    """Invalid replay identity, ordering, or state evidence."""


def _nonnegative(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ReplayError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ReplayPosition:
    slot: int
    transaction_index: int
    outer_instruction_index: int
    unix_s: int
    signature: str

    def __post_init__(self) -> None:
        for name in ("slot", "transaction_index", "outer_instruction_index", "unix_s"):
            _nonnegative(getattr(self, name), name)
        if not self.signature or self.signature != self.signature.strip():
            raise ReplayError("signature must be non-empty and trimmed")

    @property
    def transaction(self) -> tuple[int, int]:
        return self.slot, self.transaction_index

    @property
    def order(self) -> tuple[int, int, int]:
        return self.slot, self.transaction_index, self.outer_instruction_index


@dataclass(frozen=True, slots=True)
class StateObservation:
    venue_id: str
    state: QuoteState
    position: ReplayPosition
    event_id: str
    state_timing: StateTiming

    def __post_init__(self) -> None:
        if not self.venue_id or not self.event_id:
            raise ReplayError("state observation requires venue and event identity")
        if self.state_timing not in ("after_transaction", "after_outer_instruction"):
            raise ReplayError("only explicitly evidenced AFTER state is executable")


@dataclass(frozen=True, slots=True)
class ReplayOrder:
    order_id: str
    position: ReplayPosition
    venue_ids: tuple[str, ...]
    input_mint: str
    output_mint: str
    amount_in: int
    min_amount_out: int = 0
    mode: Literal["sequential", "multi_hop"] = "sequential"
    network_fee_lamports: int = 0

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ReplayError("order_id is required")
        if not isinstance(self.venue_ids, tuple) or not 1 <= len(self.venue_ids) <= 3:
            raise ReplayError("route requires one to three explicitly selected venues")
        if any(not venue for venue in self.venue_ids) or len(set(self.venue_ids)) != len(
            self.venue_ids
        ):
            raise ReplayError("route venues must be non-empty and unique")
        if not self.input_mint or not self.output_mint or self.input_mint == self.output_mint:
            raise ReplayError("route must exchange distinct explicit mint assets")
        for name in ("amount_in", "min_amount_out", "network_fee_lamports"):
            _nonnegative(getattr(self, name), name)
        if self.amount_in == 0:
            raise ReplayError("amount_in must be positive")
        if self.mode not in ("sequential", "multi_hop"):
            raise ReplayError("unknown instruction fee contract")


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    order_id: str
    status: ExecutionStatus
    input_mint: str
    output_mint: str
    amount_in: int
    amount_out: int
    network_fee_lamports: int
    route: RouteResult | None = None
    error: str | None = None


def _precedes(observation: StateObservation, execution: ReplayPosition) -> bool:
    prior = observation.position
    if prior.unix_s > execution.unix_s:
        return False
    if prior.signature == execution.signature and prior.transaction != execution.transaction:
        return False
    if prior.transaction != execution.transaction:
        return prior.transaction < execution.transaction
    if prior.signature != execution.signature:
        return False
    return (
        observation.state_timing == "after_outer_instruction"
        and prior.outer_instruction_index < execution.outer_instruction_index
    )


class PumpReplayRuntime:
    """Atomic route settlement with native-mint balances and explicit network fees.

    Fees embedded in swap quotes are not charged a second time. Creator/holder
    accruals remain venue liabilities, not simulated trader income. Unquotable
    observations do not become submitted transactions or fabricated losses.
    """

    def __init__(self, initial_balances: Mapping[str, int]) -> None:
        self._balances = dict(initial_balances)
        for mint, amount in self._balances.items():
            if not isinstance(mint, str) or not mint:
                raise ReplayError("balance requires an explicit mint")
            _nonnegative(amount, "balance")
        self._states: dict[str, StateObservation] = {}
        self._observations: dict[tuple[str, str, int, StateTiming], StateObservation] = {}
        self._event_ids: dict[str, StateObservation] = {}
        self._orders: dict[str, ReplayOrder] = {}
        self._receipts: dict[str, ExecutionReceipt] = {}
        self._transactions: dict[str, str] = {}
        self._position_signatures: dict[tuple[int, int], str] = {}
        self._last_execution: ReplayPosition | None = None

    @property
    def balances(self) -> dict[str, int]:
        return dict(self._balances)

    @property
    def receipt_count(self) -> int:
        return len(self._receipts)

    def observation(self, venue_id: str) -> StateObservation:
        try:
            return self._states[venue_id]
        except KeyError:
            raise ReplayError("missing causal venue state") from None

    def observe(self, observation: StateObservation) -> bool:
        existing = self._event_ids.get(observation.event_id)
        if existing is not None:
            if existing != observation:
                raise ReplayError("conflicting repeated source event identity")
            return False
        key = (
            observation.venue_id,
            observation.position.signature,
            observation.position.outer_instruction_index,
            observation.state_timing,
        )
        repeated = self._observations.get(key)
        if repeated is not None:
            if repeated.state != observation.state:
                raise ReplayError("conflicting repeated signed instruction state")
            return False
        latest = self._states.get(observation.venue_id)
        if latest is not None:
            old, new = latest.position, observation.position
            if new.order < old.order or new.unix_s < old.unix_s:
                raise ReplayError("state observation moves venue history backwards")
            if new.transaction == old.transaction and new.signature != old.signature:
                raise ReplayError("conflicting transaction identity at the same position")
        known_signature = self._position_signatures.get(observation.position.transaction)
        if known_signature is not None and known_signature != observation.position.signature:
            raise ReplayError("conflicting transaction identity at the same position")
        self._position_signatures[observation.position.transaction] = observation.position.signature
        self._states[observation.venue_id] = observation
        self._observations[key] = observation
        self._event_ids[observation.event_id] = observation
        return True

    def execute(self, order: ReplayOrder) -> ExecutionReceipt:
        prior = self._orders.get(order.order_id)
        if prior is not None:
            if prior != order:
                raise ReplayError("order identity reused with different execution inputs")
            return self._receipts[order.order_id]
        known_signature = self._position_signatures.get(order.position.transaction)
        if known_signature is not None and known_signature != order.position.signature:
            raise ReplayError("conflicting transaction identity at the same position")
        if order.position.signature in self._transactions:
            raise ReplayError(
                "one simulated transaction must be represented as one atomic route order"
            )
        if self._last_execution is not None and (
            order.position.order < self._last_execution.order
            or order.position.unix_s < self._last_execution.unix_s
        ):
            raise ReplayError("execution moves replay backwards")
        receipt = self._execute(order)
        self._orders[order.order_id] = order
        self._receipts[order.order_id] = receipt
        self._last_execution = order.position
        if receipt.status in ("filled", "slippage_failed"):
            self._transactions[order.position.signature] = order.order_id
            self._position_signatures[order.position.transaction] = order.position.signature
        return receipt

    def _execute(self, order: ReplayOrder) -> ExecutionReceipt:
        def result(
            status: ExecutionStatus,
            *,
            amount_in: int = 0,
            amount_out: int = 0,
            network_fee: int = 0,
            route: RouteResult | None = None,
            error: str | None = None,
        ) -> ExecutionReceipt:
            return ExecutionReceipt(
                order.order_id,
                status,
                order.input_mint,
                order.output_mint,
                amount_in,
                amount_out,
                network_fee,
                route,
                error,
            )

        observations = []
        for venue in order.venue_ids:
            observation = self._states.get(venue)
            if observation is None or not _precedes(observation, order.position):
                return result("unresolved", error="missing strictly causal AFTER state for route")
            observations.append(observation)
        states = [observation.state for observation in observations]
        by_base = {
            state.base_mint: observation.venue_id
            for state, observation in zip(states, observations, strict=True)
        }
        if len(by_base) != len(states):
            return result("unresolved", error="ambiguous selected venues for the same base mint")
        try:
            route = route_quote(
                states, order.input_mint, order.output_mint, order.amount_in, mode=order.mode
            )
        except QuoteError as error:
            return result("unresolved", error=str(error))
        if {leg.state.base_mint for leg in route.legs} != set(by_base):
            return result("rejected", error="selected route contains unused venues")
        needed = {order.input_mint: route.amount_in}
        needed[SOL_MINT] = needed.get(SOL_MINT, 0) + order.network_fee_lamports
        if any(self._balances.get(mint, 0) < amount for mint, amount in needed.items()):
            return result(
                "rejected", error="insufficient native-mint balance or SOL network reserve"
            )
        if route.amount_out < order.min_amount_out:
            self._balances[SOL_MINT] = self._balances.get(SOL_MINT, 0) - order.network_fee_lamports
            return result(
                "slippage_failed",
                network_fee=order.network_fee_lamports,
                route=route,
                error="minimum final route output not met",
            )
        balances = dict(self._balances)
        for mint, amount in needed.items():
            balances[mint] = balances.get(mint, 0) - amount
        balances[order.output_mint] = balances.get(order.output_mint, 0) + route.amount_out
        for previous, following in zip(route.legs, route.legs[1:], strict=False):
            remainder = previous.quote.amount_out - following.quote.amount_in
            if remainder < 0:
                raise ReplayError("route spends more intermediate inventory than it produces")
            if remainder:
                mint = following.input_mint
                balances[mint] = balances.get(mint, 0) + remainder
        updates = {}
        for leg in route.legs:
            venue = by_base[leg.state.base_mint]
            updates[venue] = StateObservation(
                venue,
                leg.quote.post_state,
                order.position,
                "simulated:" + order.order_id + ":" + venue,
                "after_transaction",
            )
        self._balances = balances
        self._states.update(updates)
        return result(
            "filled",
            amount_in=route.amount_in,
            amount_out=route.amount_out,
            network_fee=order.network_fee_lamports,
            route=route,
        )

    def sweep(
        self,
        venue_id: str,
        position: ReplayPosition,
        *,
        component: Literal["all", "protocol", "creator"] = "all",
    ) -> StateObservation:
        if component not in ("all", "protocol", "creator"):
            raise ReplayError("unknown fee sweep component")
        observation = self.observation(venue_id)
        if not _precedes(observation, position):
            raise ReplayError("fee sweep requires strictly causal AFTER state")
        updated = StateObservation(
            venue_id,
            sweep_fees(
                observation.state, protocol=component != "creator", creator=component != "protocol"
            ),
            position,
            "sweep:"
            + position.signature
            + ":"
            + str(position.outer_instruction_index)
            + ":"
            + venue_id,
            "after_outer_instruction",
        )
        self.observe(updated)
        return updated
