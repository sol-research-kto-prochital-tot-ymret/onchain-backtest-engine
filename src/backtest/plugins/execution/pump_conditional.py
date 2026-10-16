"""Pump protocol group reducer retaining own reserve changes in the native engine.

Historical exact-quote budgets and exact-token quantities stay fixed. Unknown
slippage constraints/reactions are not inferred. Incompatible historical seller
inventory or orders block dependent positions instead of inventing liquidity.
"""

from __future__ import annotations

from typing import Any

from backtest.domain.identifiers import AssetId, PoolId
from backtest.domain.market_events import BlockEvent
from backtest.engine.state import PoolSnapshot, VenueStateCommit
from backtest.plugins.execution.pump_sniper import SOL, curve_state, profile
from backtest.plugins.protocols.pumpfun.model import PumpQuoteError, buy_quote, sell_quote


class PumpConditionalReducer:
    def __init__(self, source: Any, execution: Any):
        self.source, self.execution = source, execution
        self.venue: Any = None
        self.intervened: set[str] = set()
        self.balances: dict[tuple[str, str], int] = {}
        self.historical_balances: dict[tuple[str, str], int] = {}
        self.totals: dict[str, int] = {}
        self.own_tokens: dict[str, int] = {}
        self.effective_rows: dict[str, dict[str, Any]] = {}
        self.errors: list[dict[str, Any]] = []
        self.repriced: list[dict[str, Any]] = []
        self.inventory_transfers = tuple(getattr(source, "inventory_transfers", ()))
        self.inventory_index = 0
        self.inventory_applied: list[dict[str, Any]] = []

    def _move_inventory(
        self,
        movement: dict[str, Any],
        balances: dict[tuple[str, str], int],
        historical: dict[tuple[str, str], int],
        applied: list[dict[str, Any]],
    ) -> None:
        """Move evidenced tokens between external wallets without creating supply."""
        mint, amount = movement["mint"], int(movement["tokens"])
        if mint in self.execution.blocked:
            return
        source = mint, movement["source_wallet"]
        destination = mint, movement["destination_wallet"]
        source_historical = historical.get(source, self.historical_balances.get(source, 0))
        source_replayed = balances.get(source, self.balances.get(source, 0))
        if source_historical < amount:
            raise ValueError("CONDITIONAL_TRANSFER_SOURCE_BALANCE_UNPROVEN")
        if source_replayed < amount:
            raise ValueError("CONDITIONAL_TRANSFER_SOURCE_INVENTORY_CONFLICT")
        balances[source] = source_replayed - amount
        balances[destination] = (
            balances.get(destination, self.balances.get(destination, 0)) + amount
        )
        historical[source] = source_historical - amount
        historical[destination] = (
            historical.get(destination, self.historical_balances.get(destination, 0)) + amount
        )
        applied.append(movement)

    @staticmethod
    def _movement_boundary(movement: dict[str, Any]) -> int:
        return (int(movement["slot"]) << 32) + int(movement["tx_idx"]) + 1

    def _earlier_inventory(self, boundary: int) -> None:
        # Transfer-only transactions have no Pump event. Apply each whole earlier
        # transaction before the next observable boundary, without looking ahead.
        while self.inventory_index < len(self.inventory_transfers):
            first = self.inventory_transfers[self.inventory_index]
            earlier = self._movement_boundary(first)
            if earlier >= boundary:
                break
            end = self.inventory_index
            group = []
            while end < len(self.inventory_transfers):
                movement = self.inventory_transfers[end]
                if self._movement_boundary(movement) != earlier:
                    break
                group.append(movement)
                end += 1
            balances: dict[tuple[str, str], int] = {}
            historical: dict[tuple[str, str], int] = {}
            applied: list[dict[str, Any]] = []
            try:
                for movement in group:
                    self._move_inventory(movement, balances, historical, applied)
            except ValueError as error:
                for mint in {m["mint"] for m in group}:
                    self.execution.blocked[mint] = str(error)
                    self.errors.append(
                        {"mint": mint, "boundary_ordinal": earlier, "reason": str(error)}
                    )
            else:
                self.balances.update(balances)
                self.historical_balances.update(historical)
                self.inventory_applied.extend(applied)
            self.inventory_index = end

    def own_fill(self, mint: str, tokens: int) -> None:
        self.intervened.add(mint)
        self.own_tokens[mint] = self.own_tokens.get(mint, 0) + tokens
        actual = self.venue.pool(PoolId(mint))
        initial = int(self.source.birth_by_mint[mint]["initial_virtual_base_raw"])
        if initial - actual.reserve_b_atomic != self.totals.get(mint, 0) + self.own_tokens[mint]:
            raise ValueError("conditional token inventory does not reconcile after our fill")

    def reconcile(self, events: Any, historical: Any, venue: Any) -> None:
        del historical
        # The bounded streaming source is used only with zero observation delay;
        # the prior group has already been delivered before this reconciliation.
        if getattr(self.source, "zero_latency_stream", False):
            self.effective_rows.clear()
        self.venue = venue
        boundary = events[0].envelope.boundary_ordinal
        self._earlier_inventory(boundary)
        movement_end = self.inventory_index
        movements = []
        while movement_end < len(self.inventory_transfers):
            movement = self.inventory_transfers[movement_end]
            if self._movement_boundary(movement) != boundary:
                break
            movements.append(movement)
            movement_end += 1
        balances: dict[tuple[str, str], int] = {}
        historical_balances: dict[tuple[str, str], int] = {}
        totals: dict[str, int] = {}
        rows: dict[str, dict[str, Any]] = {}
        logs: list[dict[str, Any]] = []
        inventory_applied: list[dict[str, Any]] = []
        movement_index = 0
        touched = {
            self.source.meta[e.envelope.canonical_event_id.hex]["row"]["mint"]
            for e in events
            if not isinstance(e, BlockEvent)
        }
        touched.update(m["mint"] for m in movements)
        # Stage only pools in this atomic transaction. The commit below is a
        # patch, so all untouched historical/own-order state stays intact.
        candidate = {
            PoolId(mint): current
            for mint in touched
            if (current := venue.pool(PoolId(mint))) is not None
        }
        try:
            for event in events:
                if isinstance(event, BlockEvent):
                    continue
                identity = event.envelope.canonical_event_id.hex
                meta = self.source.meta[identity]
                raw = meta["row"]
                path = tuple(meta.get("path", raw.get("instruction_path", ())))
                while (
                    movement_index < len(movements)
                    and tuple(movements[movement_index]["instruction_path"]) < path
                ):
                    self._move_inventory(
                        movements[movement_index], balances, historical_balances, inventory_applied
                    )
                    movement_index += 1
                mint = raw["mint"]
                if mint in self.execution.blocked:
                    continue
                if meta["kind"] == "launch":
                    b = self.source.birth_by_mint[mint]
                    candidate[PoolId(mint)] = PoolSnapshot(
                        PoolId(mint),
                        SOL,
                        AssetId(mint),
                        int(b["initial_virtual_quote_raw"]),
                        int(b["initial_virtual_base_raw"]),
                        event.envelope.boundary_ordinal,
                    )
                    continue
                if meta["kind"] != "trade":
                    continue
                key = mint, raw["trader"]
                balance = balances.get(key, self.balances.get(key, 0))
                historical_balance = historical_balances.get(
                    key, self.historical_balances.get(key, 0)
                )
                tokens = int(raw["tokens"])
                amount = int(raw["quote"])
                protocol_fee, creator_fee = int(raw["protocol_fee"]), int(raw["creator_fee"])
                current = candidate[PoolId(mint)]
                if mint in self.intervened:
                    state = curve_state(current, self.source.birth_by_mint[mint])
                    p = raw["payload"]
                    now = int(
                        p.get("timestamp")
                        or self.source.times[event.envelope.position.block_ordinal]
                    )
                    bps = int(p["fee_basis_points"]), int(p["creator_fee_basis_points"])
                    fees = profile(bps, now)
                    if raw["side"] == "sell":
                        if self.execution.policy.counterpart_sell_fraction:
                            if historical_balance < tokens or historical_balance <= 0:
                                raise ValueError("CONDITIONAL_HISTORICAL_SELLER_BALANCE_UNPROVEN")
                            tokens = balance * tokens // historical_balance
                            if tokens <= 0:
                                raise ValueError("CONDITIONAL_ZERO_SELL_REPLAY")
                        if balance < tokens:
                            raise ValueError("CONDITIONAL_SELLER_INVENTORY_CONFLICT")
                        quote = sell_quote(
                            state,
                            tokens_in_atomic=tokens,
                            fee_profile=fees,
                            effective_at_unix_s=now,
                        )
                        amount = quote.gross_curve_sol_lamports
                        protocol_fee, creator_fee = (
                            quote.fees.protocol_fee_lamports,
                            quote.fees.creator_fee_lamports,
                        )
                    elif p.get("ix_name") in ("buy_exact_quote_in", "buy_exact_sol_in"):
                        buy_result = buy_quote(
                            state,
                            spendable_gross_sol_lamports=int(raw["quote"])
                            + protocol_fee
                            + creator_fee,
                            fee_profile=fees,
                            effective_at_unix_s=now,
                        )
                        amount, tokens = (
                            buy_result.net_curve_sol_lamports,
                            buy_result.tokens_out_atomic,
                        )
                        protocol_fee, creator_fee = (
                            buy_result.fees.protocol_fee_lamports,
                            buy_result.fees.creator_fee_lamports,
                        )
                    elif p.get("ix_name") == "buy":
                        if (
                            tokens > state.real_token_reserves_atomic
                            or tokens >= state.virtual_token_reserves_atomic
                        ):
                            raise ValueError("CONDITIONAL_HISTORICAL_BUY_UNEXECUTABLE")
                        # Pump buy fixes token output; the required net SOL rounds up.
                        amount = (
                            state.virtual_sol_reserves_lamports
                            * tokens
                            // (state.virtual_token_reserves_atomic - tokens)
                            + 1
                        )
                        protocol_fee = (amount * bps[0] + 9999) // 10000
                        creator_fee = (amount * bps[1] + 9999) // 10000
                    else:
                        raise ValueError("CONDITIONAL_UNKNOWN_HISTORICAL_BUY_INTENT")
                    sign = 1 if raw["side"] == "buy" else -1
                    candidate[PoolId(mint)] = PoolSnapshot(
                        PoolId(mint),
                        SOL,
                        AssetId(mint),
                        current.reserve_a_atomic + sign * amount,
                        current.reserve_b_atomic - sign * tokens,
                        event.envelope.boundary_ordinal,
                    )
                    after = curve_state(candidate[PoolId(mint)], self.source.birth_by_mint[mint])
                    effective = dict(
                        raw,
                        quote=amount,
                        tokens=tokens,
                        protocol_fee=protocol_fee,
                        creator_fee=creator_fee,
                        virtual_quote=after.virtual_sol_reserves_lamports,
                        virtual_base=after.virtual_token_reserves_atomic,
                        real_quote=after.real_sol_reserves_lamports,
                        real_base=after.real_token_reserves_atomic,
                    )
                    rows[identity] = effective
                    logs.append(
                        {
                            "mint": mint,
                            "boundary_ordinal": event.envelope.boundary_ordinal,
                            "event_id": identity,
                            "trader": raw["trader"],
                            "side": raw["side"],
                            "historical_tokens": raw["tokens"],
                            "replayed_tokens": tokens,
                            "historical_curve_sol": raw["quote"],
                            "replayed_curve_sol": amount,
                            "real_sol_after": after.real_sol_reserves_lamports,
                        }
                    )
                else:
                    candidate[PoolId(mint)] = PoolSnapshot(
                        PoolId(mint),
                        SOL,
                        AssetId(mint),
                        int(raw["virtual_quote"]),
                        int(raw["virtual_base"]),
                        event.envelope.boundary_ordinal,
                    )
                    rows[identity] = raw
                balances[key] = balance + (tokens if raw["side"] == "buy" else -tokens)
                historical_balances[key] = historical_balance + (
                    int(raw["tokens"]) if raw["side"] == "buy" else -int(raw["tokens"])
                )
                totals[mint] = totals.get(mint, self.totals.get(mint, 0)) + (
                    tokens if raw["side"] == "buy" else -tokens
                )
                if mint in self.intervened:
                    initial = int(self.source.birth_by_mint[mint]["initial_virtual_base_raw"])
                    if initial - candidate[PoolId(mint)].reserve_b_atomic != totals[
                        mint
                    ] + self.own_tokens.get(mint, 0):
                        raise ValueError("CONDITIONAL_TOKEN_CONSERVATION_FAILURE")
            for movement in movements[movement_index:]:
                self._move_inventory(movement, balances, historical_balances, inventory_applied)
        except (PumpQuoteError, ValueError, KeyError) as error:
            reason = (
                str(error)
                if str(error).startswith("CONDITIONAL_")
                else "CONDITIONAL_HISTORICAL_ORDER_UNEXECUTABLE"
            )
            # The source transaction is atomic: do not commit a partial repricing.
            for mint in touched:
                self.execution.blocked[mint] = reason
                self.errors.append(
                    {
                        "mint": mint,
                        "boundary_ordinal": events[0].envelope.boundary_ordinal,
                        "reason": reason,
                    }
                )
            self.inventory_index = movement_end
            return
        venue.commit(VenueStateCommit(candidate, replace_all=False))
        self.balances.update(balances)
        self.historical_balances.update(historical_balances)
        self.totals.update(totals)
        self.effective_rows.update(rows)
        self.repriced.extend(logs)
        self.inventory_applied.extend(inventory_applied)
        self.inventory_index = movement_end
