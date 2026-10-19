"""Independent pinned official SDK goldens; run with PYTHONPATH=tools."""

import unittest
from dataclasses import replace

from backtest.plugins.protocols.pumpfun.quotes import (
    FeeConfig,
    InventoryError,
    MissingState,
    QuoteError,
    QuoteState,
    RouteError,
    SlippageError,
    UnsupportedInstruction,
    quote_buy_exact_quote_in,
    quote_sell_exact_base_in,
    route_quote,
    sweep_fees,
)

SOL = "So11111111111111111111111111111111111111112"


def pool(**kwargs):
    data = {
        "kind": "pool",
        "version": "v2",
        "base_mint": "A",
        "quote_mint": SOL,
        "virtual_base_reserves": 1_000_000_000,
        "virtual_quote_reserves": -50_000_000,
        "real_base_reserves": 1_000_000_000,
        "real_quote_reserves": 1_000_000_000,
        "quote_vault_amount": 1_050_000_000,
        "protocol_fees": 20_000_000,
        "creator_fees": 30_000_000,
        "fees": FeeConfig(5, 30, 20),
        "creator_beneficiary": "creator",
    }
    data.update(kwargs)
    return QuoteState(**data)


class CurrentQuoteTests(unittest.TestCase):
    def test_negative_offset_sdk_golden_not_raw_vault(self):
        state = pool()
        self.assertEqual(quote_sell_exact_base_in(state, 100_000_000).amount_out, 90_409_088)
        self.assertNotEqual(
            quote_sell_exact_base_in(state, 100_000_000).amount_out, 94_929_543
        )  # raw-vault CPM is NOT the oracle

    def test_non_sol_legacy_rejected_without_zero_fx(self):
        with self.assertRaises(UnsupportedInstruction):
            quote_buy_exact_quote_in(
                pool(
                    kind="curve",
                    version="legacy",
                    quote_mint="non-SOL",
                    virtual_quote_reserves=1_000_000_000,
                    quote_vault_amount=None,
                ),
                10_000,
            )

    def test_sdk_exact_quote_in_rounding(self):
        result = quote_buy_exact_quote_in(pool(), 100_000_000)
        self.assertEqual((result.amount_out, result.pricing_quote_amount), (90_456_804, 99_453_006))
        small = quote_buy_exact_quote_in(pool(), 10)
        self.assertEqual((small.amount_out, small.pricing_quote_amount), (5, 7))


def curve(**kwargs):
    data = {
        "kind": "curve",
        "version": "v3",
        "base_mint": "A",
        "quote_mint": SOL,
        "virtual_base_reserves": 1_073_000_000_000_000,
        "virtual_quote_reserves": 30_000_000_000,
        "real_base_reserves": 793_100_000_000_000,
        "real_quote_reserves": 10_000_000_000,
        "fees": FeeConfig(5, 30),
        "quote_vault_amount": 10_000_000_000,
        "creator_beneficiary": "creator",
    }
    data.update(kwargs)
    return QuoteState(**data)


class ArithmeticAndCapabilityTests(unittest.TestCase):
    def test_pool_fee_held_and_sweep_preserve_effective_pricing(self):
        state = pool()
        buy = quote_buy_exact_quote_in(state, 100_000_000)
        self.assertEqual(
            (buy.fees.protocol, buy.fees.creator, buy.fees.lp), (49_727, 298_360, 198_907)
        )
        self.assertEqual(
            (
                buy.post_state.quote_vault_amount,
                buy.post_state.virtual_quote_reserves,
                buy.post_state.effective_quote_reserves,
            ),
            (1_150_000_000, -50_348_087, 1_099_651_913),
        )
        swept = sweep_fees(buy.post_state)
        self.assertEqual(swept.effective_quote_reserves, buy.post_state.effective_quote_reserves)
        self.assertEqual(swept.real_quote_reserves, buy.post_state.real_quote_reserves)
        self.assertEqual(
            quote_sell_exact_base_in(swept, 100_000).amount_out,
            quote_sell_exact_base_in(buy.post_state, 100_000).amount_out,
        )

    def test_sell_sdk_golden_and_post_state(self):
        result = quote_sell_exact_base_in(pool(), 100_000_000)
        self.assertEqual(
            (
                result.pricing_quote_amount,
                result.fees.protocol,
                result.fees.creator,
                result.fees.lp,
                result.amount_out,
            ),
            (90_909_090, 45_455, 272_728, 181_819, 90_409_088),
        )
        self.assertEqual(
            (
                result.post_state.quote_vault_amount,
                result.post_state.virtual_quote_reserves,
                result.post_state.effective_quote_reserves,
            ),
            (959_590_912, -50_318_183, 909_272_729),
        )

    def test_v1_v2_same_pricing_different_fee_custody(self):
        for function in (quote_buy_exact_quote_in, quote_sell_exact_base_in):
            with self.subTest(function=function.__name__):
                v1 = function(pool(version="v1"), 100_000_000)
                v2 = function(pool(), 100_000_000)
                self.assertEqual(v1.amount_out, v2.amount_out)
                self.assertEqual(
                    v1.post_state.effective_quote_reserves, v2.post_state.effective_quote_reserves
                )
                self.assertEqual(v1.post_state.virtual_quote_reserves, -50_000_000)
                self.assertLess(v2.post_state.virtual_quote_reserves, -50_000_000)

    def test_curve_v2_v3_sdk_golden_arbitrary_quote_atomics(self):
        for mint in (SOL, "USDC", "pump-ancestor"):
            for version in ("v2", "v3"):
                with self.subTest(mint=mint, version=version):
                    result = quote_buy_exact_quote_in(
                        curve(quote_mint=mint, version=version), 1_000_000_000
                    )
                    self.assertEqual(result.amount_out, 34_496_061_650_204)
                    self.assertEqual(
                        quote_sell_exact_base_in(
                            curve(quote_mint=mint, version=version), 1_000_000_000
                        ).amount_out,
                        27_860,
                    )
        # Same atomic numbers in each mint, no fabricated decimals, supply or SOL FX.
        self.assertEqual(quote_buy_exact_quote_in(curve(), 10).amount_out, 250_366)
        self.assertEqual(quote_buy_exact_quote_in(curve(), 10_000).amount_out, 356_378_948)

    def test_curve_retention_is_not_pricing_liquidity(self):
        v2 = quote_buy_exact_quote_in(curve(version="v2"), 1_000_000_000)
        v3 = quote_buy_exact_quote_in(curve(), 1_000_000_000)
        self.assertEqual(v3.post_state.quote_vault_amount, 11_000_000_000)
        self.assertEqual(
            v3.post_state.protocol_fees + v3.post_state.creator_fees,
            v3.fees.protocol + v3.fees.creator,
        )
        self.assertEqual(
            v2.post_state.effective_quote_reserves, v3.post_state.effective_quote_reserves
        )
        self.assertEqual(
            sweep_fees(v3.post_state).effective_quote_reserves,
            v3.post_state.effective_quote_reserves,
        )

    def test_legacy_curve_sell_supported_buy_requires_instruction(self):
        self.assertEqual(
            quote_sell_exact_base_in(curve(version="legacy"), 1_000_000_000).amount_out, 27_860
        )
        with self.assertRaises(UnsupportedInstruction):
            quote_buy_exact_quote_in(curve(version="legacy"), 10_000)

    def test_missing_fields_signed_bounds_and_nonnegative_effective_q(self):
        for state in (pool(virtual_quote_reserves=None), pool(quote_vault_amount=None)):
            with self.subTest(state=state), self.assertRaises(MissingState):
                quote_buy_exact_quote_in(state, 100)
        for offset in (-(1 << 127) - 1, 1 << 127, -1_050_000_001, (1 << 64) - 1, -1_050_000_000):
            with self.subTest(offset=offset), self.assertRaises(QuoteError):
                quote_sell_exact_base_in(pool(virtual_quote_reserves=offset), 100)
        for amount in (-1, 0, 1.0, True, 1 << 64):
            with self.subTest(amount=amount), self.assertRaises(QuoteError):
                quote_buy_exact_quote_in(pool(), amount)

    def test_real_quote_inventory_not_boosted_effective_reserves(self):
        boosted = pool(virtual_quote_reserves=10_000_000_000)
        with self.assertRaises(InventoryError):
            quote_sell_exact_base_in(boosted, 1_000_000_000)
        with self.assertRaises(MissingState):
            quote_sell_exact_base_in(pool(protocol_fees=0, creator_fees=0), 100_000)

    def test_curve_inventory_crossing_is_explicit_not_old_clamp(self):
        for version, error in (("v3", MissingState), ("v2", InventoryError)):
            with self.subTest(version=version), self.assertRaises(error):
                quote_buy_exact_quote_in(curve(version=version, real_base_reserves=1), 1_000_000)
        for status in ("complete", "migrating", "unknown"):
            with self.subTest(status=status), self.assertRaises(UnsupportedInstruction):
                quote_sell_exact_base_in(curve(status=status), 1_000_000_000)
        self.assertGreater(quote_buy_exact_quote_in(pool(), 1000).amount_out, 0)

    def test_slippage_zero_liquidity_and_tiny_rounding(self):
        with self.assertRaises(SlippageError):
            quote_buy_exact_quote_in(pool(), 100_000_000, min_base_out=90_456_805)
        with self.assertRaises(SlippageError):
            quote_sell_exact_base_in(pool(), 100_000_000, min_quote_out=90_409_089)
        for state in (
            pool(virtual_quote_reserves=-1_050_000_000),
            pool(virtual_base_reserves=0, real_base_reserves=0),
        ):
            with self.assertRaises(InventoryError):
                quote_buy_exact_quote_in(state, 10_000)
        for amount in (1, 2):
            with self.assertRaises(InventoryError):
                quote_buy_exact_quote_in(pool(), amount)

    def test_holder_share_is_not_a_second_creator_fee(self):
        # Holder reward distribution already included in creator_bps by the resolver.
        result = quote_sell_exact_base_in(pool(), 100_000_000)
        self.assertEqual(result.fees.total, 500_002)
        with self.assertRaises(UnsupportedInstruction):
            FeeConfig(5, 30, contract="unknown-holder-contract")
        with self.assertRaises(UnsupportedInstruction):
            quote_buy_exact_quote_in(pool(fees=FeeConfig(5, 30, 20, buyback_bps=1)), 1000)
        with self.assertRaises(UnsupportedInstruction):
            quote_buy_exact_quote_in(pool(cashback=True), 1000)

    def test_helpers_exclude_network_fees_and_are_immutable(self):
        state = pool(fees=FeeConfig(0, 0, 0))
        result = quote_buy_exact_quote_in(state, 10_000)
        self.assertEqual((result.amount_in, result.fees.total), (10_000, 0))
        self.assertEqual(state.quote_vault_amount, 1_050_000_000)
        self.assertEqual(result.post_state.quote_vault_amount, 1_050_010_000)


class RouteTests(unittest.TestCase):
    def states(self):
        return [pool(), pool(base_mint="B", quote_mint="A"), pool(base_mint="C", quote_mint="B")]

    def test_each_sequential_leg_normal_fee_once_forward_and_reverse(self):
        states = self.states()
        result = route_quote(states, SOL, "C", 100_000_000)
        amount = 100_000_000
        for state, leg in zip(states, result.legs, strict=False):
            standalone = quote_buy_exact_quote_in(state, amount)
            self.assertEqual(leg.quote, standalone)
            self.assertEqual(leg.input_mint, state.quote_mint)
            amount = standalone.amount_out
        self.assertEqual(result.amount_out, amount)
        sold = route_quote([leg.quote.post_state for leg in result.legs], "C", SOL, amount)
        self.assertEqual(sold.legs[-1].output_mint, SOL)
        self.assertLess(sold.amount_out, 100_000_000)

    def test_official_multihop_sdk_goldens_and_masked_fees(self):
        buy = route_quote(self.states(), SOL, "C", 100_000_000, mode="multi_hop")
        self.assertEqual(
            [leg.quote.amount_out for leg in buy.legs], [90_867_785, 83_298_622, 76_540_223]
        )
        self.assertGreater(buy.legs[0].quote.fees.protocol, 0)
        self.assertEqual(buy.legs[0].quote.fees.creator, 0)
        self.assertEqual(buy.legs[1].quote.fees.total, 0)
        self.assertEqual(buy.legs[2].quote.fees.protocol, 0)
        self.assertGreater(buy.legs[2].quote.fees.lp, 0)
        self.assertGreater(buy.legs[2].quote.fees.creator, 0)
        sell = route_quote(self.states(), "C", SOL, 100_000_000, mode="multi_hop")
        self.assertEqual(
            [leg.quote.amount_out for leg in sell.legs], [90_454_543, 82_951_227, 76_559_081]
        )
        self.assertGreater(sell.legs[0].quote.fees.creator, 0)
        self.assertEqual(sell.legs[1].quote.fees.total, 0)
        self.assertGreater(sell.legs[2].quote.fees.protocol, 0)
        # Route-local mask must not overwrite post-state's full future fee schedule.
        self.assertEqual(buy.legs[1].quote.post_state.fees, self.states()[1].fees)

    def test_depth_missing_chain_cycles_and_ambiguity(self):
        for states, start, end, kw in (
            (self.states(), SOL, "C", {"max_depth": 2}),
            ([pool(quote_mint="B"), pool(base_mint="B", quote_mint="A")], SOL, "A", {}),
            ([pool(quote_mint="missing")], SOL, "A", {}),
            ([pool(), pool(version="v1")], SOL, "A", {}),
            (self.states(), SOL, SOL, {}),
        ):
            with self.subTest(start=start, end=end, kw=kw), self.assertRaises(RouteError):
                route_quote(states, start, end, 100_000, **kw)

    def test_route_final_slippage_and_capability_gates(self):
        with self.assertRaises(SlippageError):
            route_quote(self.states(), SOL, "C", 100_000_000, min_amount_out=100_000_000)
        for state in (pool(mayhem=True), pool(canonical=False), pool(version="v1")):
            with self.subTest(state=state), self.assertRaises(UnsupportedInstruction):
                route_quote([state], SOL, "A", 100_000, mode="multi_hop")


class SyntheticTests(unittest.TestCase):
    def state(self):
        return curve(
            virtual_base_reserves=10000,
            virtual_quote_reserves=10000,
            real_base_reserves=1000,
            real_quote_reserves=1000,
            quote_vault_amount=1000,
            curve_base_token_balance=6000,
            pool_migration_fee=100,
        )

    def test_sdk_crossing_golden_and_post_state(self):
        # Independently executed @pump-fun/pump-sdk 4.1.0 export returned 3411.
        result = quote_buy_exact_quote_in(self.state(), 3000)
        self.assertEqual(result.amount_out, 3411)
        execution = result.synthetic
        self.assertIsNotNone(execution)
        self.assertEqual(
            (execution.curve_quote_in, execution.pool_base_before, execution.pool_quote_before),
            (1112, 5000, 2012),
        )
        self.assertEqual((execution.pool_base_after, execution.pool_quote_after), (2589, 3888))
        self.assertEqual(result.fees.total, 12)
        self.assertEqual(result.post_state.status, "complete")
        self.assertEqual(result.post_state.curve_base_token_balance, 2589)
        self.assertEqual(result.post_state.post_complete_base_out, 2411)
        with self.assertRaises(UnsupportedInstruction):
            quote_sell_exact_base_in(result.post_state, 100)
        swept = sweep_fees(result.post_state)
        self.assertEqual(swept.quote_vault_amount, 3988)

    def test_sdk_exact_output_crossing_and_maximum(self):
        from backtest.plugins.protocols.pumpfun.quotes import quote_buy_exact_base_out

        # Independently executed official SDK exact-output export returned 2982.
        result = quote_buy_exact_base_out(self.state(), 3400)
        self.assertEqual((result.amount_in, result.amount_out), (2982, 3400))
        self.assertEqual(result.synthetic.pool_base_after, 2600)
        with self.assertRaises(SlippageError):
            quote_buy_exact_base_out(self.state(), 3400, max_quote_in=2981)
        with self.assertRaises(InventoryError):
            quote_buy_exact_base_out(self.state(), 6000)
        legacy = quote_buy_exact_base_out(curve(version="legacy"), 1000)
        self.assertEqual(legacy.amount_out, 1000)

    def test_missing_inventory_fee_and_beneficiary(self):
        with self.assertRaises(MissingState):
            quote_buy_exact_quote_in(curve(real_base_reserves=1), 1000000)
        with self.assertRaises(MissingState):
            quote_buy_exact_quote_in(replace(self.state(), pool_migration_fee=None), 3000)
        with self.assertRaises(MissingState):
            quote_buy_exact_quote_in(pool(creator_beneficiary="unknown"), 1000)
        result = quote_buy_exact_quote_in(pool(creator_beneficiary="holder_rewards"), 10000)
        self.assertEqual(result.post_state.creator_beneficiary, "holder_rewards")
        self.assertGreater(result.fees.creator, 0)

    def test_token_quote_no_sol_migration_fee_and_route_crossing(self):
        state = replace(self.state(), quote_mint="TOKEN", pool_migration_fee=None)
        result = quote_buy_exact_quote_in(state, 3000)
        self.assertEqual(result.synthetic.pool_quote_before, 2112)
        route = route_quote(
            [self.state(), pool(base_mint="B", quote_mint="A")], SOL, "B", 3000, mode="multi_hop"
        )
        self.assertIsNotNone(route.legs[0].quote.synthetic)
        self.assertEqual(route.legs[0].quote.post_state.status, "complete")
        self.assertGreater(route.amount_out, 0)
