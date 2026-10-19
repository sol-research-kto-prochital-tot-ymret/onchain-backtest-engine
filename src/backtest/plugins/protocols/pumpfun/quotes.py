"""Causal native-mint integer Pump quotes, pinned to public docs 639b87fb45c3.

Arithmetic: @pump-fun/pump-sdk 4.1.0 bondingCurve.ts 789-982;
@pump-fun/pump-swap-sdk 2.1.0 multiHop.ts 245-304.
No account version, fee tier, FX, supply or activation date is inferred.
FeeConfig must already resolve mint class, tiers, overrides and creator presence.
Nonzero buyback splits and cashback payout contracts fail closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

U64_MAX = (1 << 64) - 1
I128_MIN, I128_MAX = -(1 << 127), (1 << 127) - 1
SOL_MINT = "So11111111111111111111111111111111111111112"
SOL_LIKE = frozenset((SOL_MINT, "11111111111111111111111111111111"))


class QuoteError(ValueError):
    """State or instruction is not quotable without guessing."""


class MissingState(QuoteError):
    pass


class UnsupportedInstruction(QuoteError):
    pass


class SyntheticMigrationUnsupported(UnsupportedInstruction):
    pass


class InventoryError(QuoteError):
    pass


class SlippageError(QuoteError):
    pass


class RouteError(QuoteError):
    pass


def _uint(value: int, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or not (int(positive) <= value <= U64_MAX):
        raise QuoteError(f"{name}: expected {'positive ' if positive else ''}u64 atomics")
    return value


@dataclass(frozen=True)
class FeeConfig:
    """Already resolved effective rates; no market-cap/supply/SOL assumptions."""

    protocol_bps: int
    creator_bps: int
    lp_bps: int = 0
    contract: str = "resolved-pump-fees"
    buyback_bps: int = 0

    def __post_init__(self) -> None:
        if self.contract != "resolved-pump-fees":
            raise UnsupportedInstruction("unknown fee contract; resolve from causal fee state")
        for name in ("protocol_bps", "creator_bps", "lp_bps", "buyback_bps"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 10_000:
                raise QuoteError(f"{name}: invalid basis points")
        if self.protocol_bps + self.creator_bps + self.lp_bps >= 10_000:
            raise QuoteError("total trading fee must be below 100%")
        if self.buyback_bps > self.protocol_bps:
            raise QuoteError("buyback must be a subset of protocol fee")


@dataclass(frozen=True)
class FeeAmounts:
    protocol: int
    creator: int
    lp: int
    buyback: int = 0

    @property
    def total(self) -> int:
        return self.protocol + self.creator + self.lp

    @property
    def held(self) -> int:
        return self.protocol - self.buyback + self.creator


@dataclass(frozen=True)
class QuoteState:
    kind: Literal["curve", "pool"]
    version: str
    base_mint: str
    quote_mint: str
    virtual_base_reserves: int
    virtual_quote_reserves: int | None
    real_base_reserves: int
    real_quote_reserves: int
    fees: FeeConfig
    quote_vault_amount: int | None = None
    status: str = "active"
    protocol_fees: int = 0
    creator_fees: int = 0
    cashback: bool = False
    mayhem: bool = False
    canonical: bool = True
    creator_beneficiary: Literal["creator", "holder_rewards", "none", "unknown"] = "unknown"
    curve_base_token_balance: int | None = None
    pool_migration_fee: int | None = None
    post_complete_base_out: int = 0
    post_complete_quote_in: int = 0

    @property
    def effective_quote_reserves(self) -> int:
        if self.virtual_quote_reserves is None:
            raise MissingState("virtual_quote_reserves is required; missing is not zero")
        if self.kind == "pool":
            if self.quote_vault_amount is None:
                raise MissingState("quote_vault_amount required for pool effective reserves")
            return self.quote_vault_amount + self.virtual_quote_reserves
        return self.virtual_quote_reserves

    @property
    def retains_fees(self) -> bool:
        return (self.kind, self.version) in (("curve", "v3"), ("pool", "v2"))


def _validate(state: QuoteState) -> None:
    allowed = {"curve": ("legacy", "v2", "v3"), "pool": ("legacy", "v1", "v2")}
    if state.kind not in allowed or state.version not in allowed[state.kind]:
        raise UnsupportedInstruction("unknown kind/instruction version")
    if state.status != "active":
        raise UnsupportedInstruction(f"{state.status}: need causal active pool after migration")
    if not state.base_mint or not state.quote_mint or state.base_mint == state.quote_mint:
        raise MissingState("distinct base_mint and quote_mint required")
    if state.kind == "curve" and state.version == "legacy" and state.quote_mint not in SOL_LIKE:
        raise UnsupportedInstruction("legacy curve instruction cannot trade arbitrary quote mint")
    if state.creator_beneficiary not in ("creator", "holder_rewards", "none", "unknown"):
        raise MissingState("unknown creator fee beneficiary")
    if state.fees.creator_bps and state.creator_beneficiary in ("unknown", "none"):
        raise MissingState("nonzero creator fee requires explicit beneficiary")
    if state.cashback:
        # Cashback needs a distinct payout contract; never treat it as retained creator fees.
        raise UnsupportedInstruction("cashback fee payout contract not implemented")
    if not isinstance(state.fees, FeeConfig):
        raise MissingState("resolved FeeConfig required")
    for name in (
        "virtual_base_reserves",
        "real_base_reserves",
        "real_quote_reserves",
        "protocol_fees",
        "creator_fees",
    ):
        _uint(getattr(state, name), name)
    offset = state.virtual_quote_reserves
    if offset is None:
        raise MissingState("signed virtual_quote_reserves missing; not safe to assume zero")
    if type(offset) is not int or not I128_MIN <= offset <= I128_MAX:
        raise QuoteError("virtual_quote_reserves outside signed i128")
    effective = _uint(state.effective_quote_reserves, "effective quote reserves")
    if state.virtual_base_reserves == 0 or effective == 0:
        raise InventoryError("zero pricing reserves; no fabricated FX")
    if state.real_base_reserves > state.virtual_base_reserves:
        raise InventoryError("real base exceeds pricing base reserves")
    if state.kind == "pool":
        assert state.quote_vault_amount is not None
        _uint(state.quote_vault_amount, "quote vault")
        if state.virtual_base_reserves != state.real_base_reserves:
            raise InventoryError("pool base pricing must equal real base vault inventory")
        if state.quote_vault_amount != (
            state.real_quote_reserves + state.protocol_fees + state.creator_fees
        ):
            raise MissingState("pool vault must equal real quote + known held fee buckets")
    elif state.fees.lp_bps:
        raise UnsupportedInstruction("bonding curves do not charge LP fees")
    elif state.real_quote_reserves > effective:
        raise InventoryError("curve real quote exceeds virtual pricing quote")
    if state.kind == "curve" and state.quote_vault_amount is not None:
        _uint(state.quote_vault_amount, "curve quote vault (exclude rent)")
        if state.quote_vault_amount != (
            state.real_quote_reserves
            + state.protocol_fees
            + state.creator_fees
            + state.post_complete_quote_in
        ):
            raise MissingState("curve quote vault must exclude rent and identify held fee buckets")


def validate_state(state: QuoteState) -> None:
    """Validate a causal snapshot without enabling trades on completed curves.

    FeeConfig is an assertion of resolved rates, including a resolved zero
    buyback split; unknown fee resolution must not construct a FeeConfig.
    """
    if state.status not in ("active", "complete"):
        raise UnsupportedInstruction("unknown or migrating state cannot be validated")
    if state.status == "complete" and state.kind != "curve":
        raise UnsupportedInstruction("only curves have a complete lifecycle")
    _validate(replace(state, status="active"))


def _fee(amount: int, bps: int) -> int:
    return (amount * bps + 9_999) // 10_000


def _fees(amount: int, config: FeeConfig) -> FeeAmounts:
    protocol = _fee(amount, config.protocol_bps)
    # A split of an already-rounded protocol fee: buyback is paid immediately.
    if config.buyback_bps:
        raise UnsupportedInstruction("nonzero buyback split rounding not established by quote SDK")
    buyback = 0
    return FeeAmounts(
        protocol, _fee(amount, config.creator_bps), _fee(amount, config.lp_bps), buyback
    )


@dataclass(frozen=True)
class QuoteResult:
    amount_in: int
    amount_out: int
    pricing_quote_amount: int
    fees: FeeAmounts
    post_state: QuoteState
    synthetic: SyntheticExecution | None = None


def _post(
    state: QuoteState, base_delta: int, real_quote_delta: int, fees: FeeAmounts
) -> QuoteState:
    retained = fees.held if state.retains_fees else 0
    offset = state.virtual_quote_reserves
    assert offset is not None
    vault = state.quote_vault_amount
    post = replace(
        state,
        virtual_base_reserves=state.virtual_base_reserves + base_delta,
        real_base_reserves=state.real_base_reserves + base_delta,
        real_quote_reserves=state.real_quote_reserves + real_quote_delta,
        protocol_fees=state.protocol_fees
        + (fees.protocol - fees.buyback if state.retains_fees else 0),
        creator_fees=state.creator_fees + (fees.creator if state.retains_fees else 0),
        quote_vault_amount=None if vault is None else vault + real_quote_delta + retained,
        virtual_quote_reserves=(
            offset - retained if state.kind == "pool" else offset + real_quote_delta
        ),
        status=(
            "complete"
            if state.kind == "curve" and state.real_base_reserves + base_delta == 0
            else state.status
        ),
        curve_base_token_balance=(
            None
            if state.curve_base_token_balance is None
            else state.curve_base_token_balance + base_delta
        ),
    )
    _validate(replace(post, status="active"))
    return post


def quote_buy_exact_quote_in(
    state: QuoteState, amount: int, *, min_base_out: int = 0
) -> QuoteResult:
    """SDK exact-in sizing; all fees once, in quote atomics, no network fee."""
    _validate(state)
    _uint(amount, "amount", positive=True)
    _uint(min_base_out, "min_base_out")
    if state.kind == "curve" and state.version == "legacy":
        raise UnsupportedInstruction("legacy curve has no exact-quote-in instruction; use v2/v3")
    total_bps = state.fees.protocol_bps + state.fees.creator_bps + state.fees.lp_bps
    net = amount * 10_000 // (10_000 + total_bps)
    fees = _fees(net, state.fees)
    net -= max(0, net + fees.total - amount)  # do NOT recompute fees after shave
    if net <= 1:
        raise InventoryError("budget buys no base after fees and rounding")
    priced = net - 1
    out = priced * state.virtual_base_reserves // (state.effective_quote_reserves + priced)
    return _buy_result(state, amount, out, net, fees, min_base_out)


def _buy_result(
    state: QuoteState, amount: int, out: int, net: int, fees: FeeAmounts, min_out: int
) -> QuoteResult:
    if out > state.real_base_reserves:
        if state.kind == "curve" and state.version == "v3" and not state.mayhem:
            return _synthetic_buy(state, amount, min_out)
        raise InventoryError("buy exceeds real base inventory; partial fill not requested")
    if out <= 0:
        raise InventoryError("trade rounds to zero base")
    if out < min_out:
        raise SlippageError("base output below minimum")
    # Exact budget is spent; LP fee and any division dust remain pricing liquidity.
    real_delta = amount - fees.protocol - fees.creator
    return QuoteResult(amount, out, net, fees, _post(state, -out, real_delta, fees))


def quote_sell_exact_base_in(
    state: QuoteState, amount: int, *, min_quote_out: int = 0
) -> QuoteResult:
    _validate(state)
    _uint(amount, "amount", positive=True)
    _uint(min_quote_out, "min_quote_out")
    gross = amount * state.effective_quote_reserves // (state.virtual_base_reserves + amount)
    fees = _fees(gross, state.fees)
    # LP fee stays in reserves; protocol/creator cannot be spent twice from old buckets.
    real_out = gross - fees.lp
    if real_out > state.real_quote_reserves:
        raise InventoryError("sell exceeds spendable real quote inventory (not effective Q)")
    out = gross - fees.total
    if out <= 0:
        raise InventoryError("trade rounds to zero quote after fees")
    if out < min_quote_out:
        raise SlippageError("quote output below minimum")
    return QuoteResult(amount, out, gross, fees, _post(state, amount, -real_out, fees))


def sweep_fees(state: QuoteState, *, protocol: bool = True, creator: bool = True) -> QuoteState:
    """Sweep selected held fee buckets without changing pricing liquidity."""
    _validate(replace(state, status="active"))
    if type(protocol) is not bool or type(creator) is not bool or not (protocol or creator):
        raise QuoteError("select protocol and/or creator fee sweep")
    protocol_paid = state.protocol_fees if protocol else 0
    creator_paid = state.creator_fees if creator else 0
    held = protocol_paid + creator_paid
    assert state.virtual_quote_reserves is not None
    if state.kind == "pool":
        assert state.quote_vault_amount is not None
        return replace(
            state,
            quote_vault_amount=state.quote_vault_amount - held,
            virtual_quote_reserves=state.virtual_quote_reserves + held,
            protocol_fees=state.protocol_fees - protocol_paid,
            creator_fees=state.creator_fees - creator_paid,
        )
    return replace(
        state,
        quote_vault_amount=(
            None if state.quote_vault_amount is None else state.quote_vault_amount - held
        ),
        protocol_fees=state.protocol_fees - protocol_paid,
        creator_fees=state.creator_fees - creator_paid,
    )


@dataclass(frozen=True)
class RouteLeg:
    input_mint: str
    output_mint: str
    state: QuoteState
    quote: QuoteResult


@dataclass(frozen=True)
class RouteResult:
    amount_in: int
    amount_out: int
    legs: tuple[RouteLeg, ...]


def route_quote(
    states: Sequence[QuoteState],
    input_mint: str,
    output_mint: str,
    amount_in: int,
    *,
    max_depth: int = 3,
    min_amount_out: int = 0,
    mode: str = "sequential",
) -> RouteResult:
    """Compose supplied causal ancestor states, forward buys or reverse sells.

    max_depth counts trade legs (official curve depth 2 implies three legs).
    sequential: individual instructions, each leg's full fee once.
    multi_hop: official single instruction: protocol at currency end, creator/LP
    at target end, zero elsewhere. Only modern canonical non-mayhem hops.
    No cross-route/transaction/network fees here. Unrelated states are ignored;
    duplicate venues for the same base are ambiguous: caller selects live venue.
    """
    _uint(amount_in, "amount_in", positive=True)
    _uint(min_amount_out, "min_amount_out")
    if mode not in ("sequential", "multi_hop"):
        raise UnsupportedInstruction("unknown route fee/instruction contract")
    if type(max_depth) is not int or not 1 <= max_depth <= 3:
        raise RouteError("max_depth must be 1..3 legs for current depth-2 capability")
    if input_mint == output_mint:
        raise RouteError("route must change mint; no zero-FX identity route")
    by_base: dict[str, QuoteState] = {}
    for state in states:
        if state.base_mint in by_base:
            raise RouteError("ambiguous live venues for base mint")
        by_base[state.base_mint] = state

    def ancestors(start: str, end: str) -> list[QuoteState] | None:
        path: list[QuoteState] = []
        seen: set[str] = set()
        mint = start
        while mint != end:
            if mint in seen:
                raise RouteError("ancestor cycle")
            seen.add(mint)
            state = by_base.get(mint)
            if state is None:
                return None
            path.append(state)
            if len(path) > max_depth:
                raise RouteError("ancestor route exceeds depth capability")
            mint = state.quote_mint
        return path

    # Find buy path first, without trying to mark a quote coin at a fixed FX.
    path = ancestors(output_mint, input_mint)
    is_buy = path is not None
    if path is not None:
        path.reverse()
    else:
        path = ancestors(input_mint, output_mint)
    if not path:
        raise RouteError("missing causal ancestor route; mixed-direction routes unsupported")
    amount, mint = amount_in, input_mint
    legs: list[RouteLeg] = []
    for index, state in enumerate(path):
        _validate(state)
        effective = state
        if mode == "multi_hop":
            if state.mayhem or (state.kind == "pool" and not state.canonical):
                raise UnsupportedInstruction("multi-hop requires canonical non-mayhem venues")
            if not state.retains_fees:
                raise UnsupportedInstruction("multi-hop needs curve v3 / pool v2 fee retention")
            currency_end = index == (0 if is_buy else len(path) - 1)
            target_end = index == (len(path) - 1 if is_buy else 0)
            effective = replace(
                state,
                fees=replace(
                    state.fees,
                    protocol_bps=state.fees.protocol_bps if currency_end else 0,
                    buyback_bps=state.fees.buyback_bps if currency_end else 0,
                    creator_bps=state.fees.creator_bps if target_end else 0,
                    lp_bps=state.fees.lp_bps if target_end else 0,
                ),
            )
        if is_buy:
            quote = quote_buy_exact_quote_in(effective, amount)
            if mode == "multi_hop" and quote.synthetic is not None:
                quote = _synthetic_buy(effective, amount, 0, whole_budget=True)
            elif mode == "multi_hop":
                # Official multiHop.ts spends the whole residual, not SDK sizing's net.
                net = amount - quote.fees.total
                priced = net - 1
                out = (
                    priced
                    * state.virtual_base_reserves
                    // (state.effective_quote_reserves + priced)
                )
                quote = _buy_result(effective, amount, out, net, quote.fees, 0)
            next_mint = state.base_mint
        else:
            quote = quote_sell_exact_base_in(effective, amount)
            next_mint = state.quote_mint
        # Restore the full schedule in post-state; masked fees are route-local only.
        quote = replace(quote, post_state=replace(quote.post_state, fees=state.fees))
        legs.append(RouteLeg(mint, next_mint, state, quote))
        amount, mint = quote.amount_out, next_mint
    if amount < min_amount_out:
        raise SlippageError("final route output below minimum")
    return RouteResult(legs[0].quote.amount_in, amount, tuple(legs))


@dataclass(frozen=True)
class SyntheticExecution:
    """Pool-to-be, not a live pool: migration must be observed before trading it."""

    curve_base_out: int
    curve_quote_in: int
    pool_base_before: int
    pool_quote_before: int
    pool_base_after: int
    pool_quote_after: int
    pool_base_out: int
    pool_quote_in: int
    curve_fees: FeeAmounts
    pool_fees: FeeAmounts


def _net_budget(amount: int, config: FeeConfig) -> tuple[int, FeeAmounts]:
    net = amount * 10_000 // (10_000 + config.protocol_bps + config.creator_bps)
    fees = _fees(net, config)
    return net - max(0, net + fees.total - amount), fees


def _synthetic_buy(
    state: QuoteState,
    amount: int,
    min_out: int,
    *,
    whole_budget: bool = False,
    exact_base_out: int | None = None,
) -> QuoteResult:
    balance = state.curve_base_token_balance
    if balance is None:
        raise MissingState("synthetic buy requires causal curve base ATA balance")
    _uint(balance, "curve base balance")
    remaining = state.real_base_reserves
    denominator = state.virtual_base_reserves - remaining
    if denominator <= 0:
        raise InventoryError("curve completion has no finite cost")
    # SDK getBuySolAmountFromTokenAmountQuote: floor(Q*t/(B-t)) + 1.
    curve_net = state.effective_quote_reserves * remaining // denominator + 1
    curve_fees = _fees(curve_net, state.fees)
    curve_cost = curve_net + curve_fees.total
    left = amount - curve_cost
    if left < 0:
        raise InventoryError("budget cannot complete curve")
    migration_fee = 0
    if state.quote_mint in SOL_LIKE:
        if state.pool_migration_fee is None:
            raise MissingState("SOL synthetic buy requires causal pool migration fee")
        migration_fee = _uint(state.pool_migration_fee, "migration fee")
    pool_base = balance - remaining
    pool_quote = state.real_quote_reserves + curve_net - migration_fee
    if pool_base <= 0 or pool_quote <= 0:
        raise InventoryError("empty pool-to-be")
    leg_net, leg_fees = _net_budget(left, state.fees)
    if whole_budget:
        leg_net = left - leg_fees.total
    extra = (leg_net - 1) * pool_base // (pool_quote + leg_net - 1) if leg_net > 1 else 0
    # Direct v3 leaves unspendable residual with user, per SYNTHETIC_MIGRATION.
    consumed = amount if extra > 0 or whole_budget else curve_cost
    if not extra and not whole_budget:
        leg_net, leg_fees = 0, FeeAmounts(0, 0, 0)
    if exact_base_out is not None:
        extra = exact_base_out - remaining
        if not 0 < extra < pool_base:
            raise InventoryError("synthetic exact output takes whole pool-to-be")
        leg_net = (pool_quote * extra + pool_base - extra - 1) // (pool_base - extra)
        leg_fees = _fees(leg_net, state.fees)
        consumed = curve_cost + leg_net + leg_fees.total
    fees = FeeAmounts(
        curve_fees.protocol + leg_fees.protocol, curve_fees.creator + leg_fees.creator, 0
    )
    out = remaining + extra
    if out < min_out:
        raise SlippageError("synthetic total base output below minimum")
    quote_delta = consumed - fees.total
    # Curve accounting remains at completion; post-complete legs have separate counters.
    post = _post(state, -remaining, curve_net, curve_fees)
    post = replace(
        post,
        curve_base_token_balance=balance - out,
        quote_vault_amount=(
            None if state.quote_vault_amount is None else state.quote_vault_amount + consumed
        ),
        protocol_fees=state.protocol_fees + fees.protocol,
        creator_fees=state.creator_fees + fees.creator,
        post_complete_base_out=extra,
        post_complete_quote_in=quote_delta - curve_net,
    )
    execution = SyntheticExecution(
        remaining,
        curve_net,
        pool_base,
        pool_quote,
        pool_base - extra,
        pool_quote + quote_delta - curve_net,
        extra,
        quote_delta - curve_net,
        curve_fees,
        leg_fees,
    )
    for value in (out, quote_delta, execution.pool_base_after, execution.pool_quote_after):
        _uint(value, "synthetic result")
    _uint(
        post.curve_base_token_balance if post.curve_base_token_balance is not None else 0,
        "remaining physical curve base",
    )
    _validate(replace(post, status="active"))
    return QuoteResult(consumed, out, curve_net + leg_net, fees, post, execution)


def quote_buy_exact_base_out(
    state: QuoteState, amount: int, *, max_quote_in: int = U64_MAX
) -> QuoteResult:
    """Legacy buy / buy_v2 / buy_v3; no implicit partial-fill request."""
    _validate(state)
    _uint(amount, "base output", positive=True)
    _uint(max_quote_in, "max_quote_in")
    if amount > state.real_base_reserves:
        if state.kind != "curve" or state.version != "v3" or state.mayhem:
            raise InventoryError("exact output exceeds inventory")
        result = _synthetic_buy(state, U64_MAX, 0, exact_base_out=amount)
    else:
        denominator = state.virtual_base_reserves - amount
        if denominator <= 0:
            raise InventoryError("exact output exhausts pricing inventory")
        numerator = state.effective_quote_reserves * amount
        net = (
            numerator // denominator + 1
            if state.kind == "curve"
            else (numerator + denominator - 1) // denominator
        )
        fees = _fees(net, state.fees)
        cost = net + fees.total
        result = QuoteResult(cost, amount, net, fees, _post(state, -amount, net + fees.lp, fees))
    _uint(result.amount_in, "quote input")
    if result.amount_in > max_quote_in:
        raise SlippageError("quote cost above maximum")
    return result
