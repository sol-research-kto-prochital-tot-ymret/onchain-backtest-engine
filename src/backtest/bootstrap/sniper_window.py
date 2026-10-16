"""Compose the fork's native reference engine with the Sniper Window plugin."""

from __future__ import annotations

import csv
import json
import os
import platform
import time
from collections import Counter
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from backtest.adapters.artifacts.dnipro import publish_result
from backtest.adapters.source.sniper_silver import SilverSniperSource, file_hash
from backtest.domain.execution import ExecutionMode, Fill
from backtest.domain.hashing import canonical_json_bytes
from backtest.domain.ledger import LedgerTransaction
from backtest.engine.audit import fill_document, ledger_document
from backtest.engine.reference import ReferenceBacktestEngine, ReferenceRunConfig, SlotLatencyModel
from backtest.plugins.execution.pump_sniper import SOL, PumpSniperExecutionModel
from backtest.plugins.strategies.sniper_window import (
    SniperWindowPolicy,
    SniperWindowRisk,
    SniperWindowStrategy,
)


class ResearchSink:
    def __init__(self) -> None:
        self.ledger: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []
        self.audit_counts: Counter[str] = Counter()
        self.failures: list[dict[str, object]] = []
        self.creator_fee_recipients: list[dict[str, Any]] = []
        self.strategy_trace: list[dict[str, Any]] = []
        self.order_audit: list[dict[str, object]] = []
        self.conditional_errors: list[dict[str, Any]] = []
        self.conditional_repriced: list[dict[str, Any]] = []
        self.inventory_transfers_applied: list[dict[str, Any]] = []
        self.last_progress = time.monotonic()

    def append_audit(self, record: dict[str, object]) -> None:
        kind = str(record["record_type"])
        self.audit_counts[kind] += 1
        if kind.startswith("ORDER_"):
            self.order_audit.append(record)
        if kind in ("ORDER_REJECTED", "ORDER_EXECUTION_FAILED_AND_RELEASED"):
            self.failures.append(record)
        if time.monotonic() - self.last_progress >= 20:
            print(
                f"Native engine: {self.audit_counts['HISTORICAL_GROUP_APPLIED']:,} "
                "historical groups processed",
                flush=True,
            )
            self.last_progress = time.monotonic()

    def append_ledger(self, transaction: LedgerTransaction) -> None:
        self.ledger.append(ledger_document(transaction))

    def append_fill(self, fill: Fill) -> None:
        self.fills.append(fill_document(fill))


def sol_string(lamports: int) -> str:
    return format(Decimal(lamports) / Decimal(1_000_000_000), ".9f")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(
            {
                k: json.dumps(v, sort_keys=True) if isinstance(v, (list, dict)) else v
                for k, v in row.items()
            }
            for row in rows
        )


def implementation_identity() -> dict[str, str]:
    import hashlib

    h = hashlib.sha256()
    root = Path(__file__).resolve().parents[1]
    for path in sorted(root.rglob("*.py")):
        h.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return {
        "engine_source_sha256": h.hexdigest(),
        "python": platform.python_version(),
        "runner": "backtest.engine.reference.ReferenceBacktestEngine",
        "execution_model": "backtest.plugins.protocols.pumpfun.model",
        "accounting": "backtest.engine.portfolio.PortfolioState",
    }


def run(
    source: SilverSniperSource,
    policy: SniperWindowPolicy,
    execution_mode: ExecutionMode = ExecutionMode.EXOGENOUS_REPLAY,
    *,
    trace_mints: set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], ResearchSink]:
    execution = PumpSniperExecutionModel(source.birth_by_mint, policy, execution_mode)
    if execution_mode is ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY:
        from backtest.plugins.execution.pump_conditional import PumpConditionalReducer

        execution.conditional = PumpConditionalReducer(source, execution)
    strategy = SniperWindowStrategy(source, execution, policy)
    strategy.trace_mints = trace_mints or set()
    sink = ResearchSink()
    config = ReferenceRunConfig(
        execution_mode,
        SlotLatencyModel(observation_slots=0, order_slots=policy.entry_delay_slots),
        20261010,
        {SOL: policy.bankroll_lamports},
        protocol_reducer_enabled=execution_mode is ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY,
    )
    identity = implementation_identity()
    result = ReferenceBacktestEngine().run(
        source=source,
        strategy=strategy,
        execution_model=execution,
        risk_policy=SniperWindowRisk(),
        config=config,
        sink=sink,
    )
    if implementation_identity() != identity:
        raise ValueError("engine source changed during the run; rerun with stable code")
    rows = list(strategy.rows.values())
    sink.strategy_trace = strategy.trace
    if execution.conditional is not None:
        sink.conditional_errors = execution.conditional.errors
        sink.conditional_repriced = execution.conditional.repriced
        sink.inventory_transfers_applied = execution.conditional.inventory_applied
    strategy.creator_income.verify()
    for (mint, wallet), amount in sorted(strategy.creator_income.by_wallet.items()):
        trader = strategy.wallets[mint][wallet]
        sink.creator_fee_recipients.append(
            {
                "mint": mint,
                "symbol": strategy.rows[mint]["symbol"],
                "token_gmgn": strategy.rows[mint]["gmgn_url"],
                "fee_recipient": wallet,
                "wallet_gmgn": "https://gmgn.ai/sol/address/" + wallet,
                "is_launch_creator": wallet == strategy.rows[mint]["creator"],
                "creator_fee_accrual_lamports": amount,
                "creator_fee_accrual_sol": sol_string(amount),
                "observed_net_trade_cost_lamports": trader["cost"],
                "observed_remaining_tokens_raw": trader["tokens"],
                "trade_cashflow_plus_fee_accrual_lamports": -trader["cost"] + amount,
                "trade_cashflow_plus_fee_accrual_sol": sol_string(-trader["cost"] + amount),
                "income_kind": "event-time-accrual-not-proven-withdrawal",
            }
        )
    for row in rows:
        mint, creator = row["mint"], row["creator"]
        supported = mint not in source.unsupported
        ledger = strategy.wallets[mint][creator]
        row.update(
            developer_observed_net_trade_cost_lamports=ledger["cost"] if supported else None,
            developer_observed_tokens_raw=ledger["tokens"] if supported else None,
            developer_creator_fee_accrual_lamports=strategy.creator_income.income(mint, creator)
            if supported
            else None,
            developer_trade_cashflow_plus_fee_accrual_lamports=(
                -ledger["cost"] + strategy.creator_income.income(mint, creator)
            )
            if supported
            else None,
            token_total_creator_fee_accrual_lamports=strategy.creator_income.total.get(mint, 0)
            if supported
            else None,
            token_unallocated_creator_fee_accrual_lamports=strategy.creator_income.unallocated.get(
                mint, 0
            )
            if supported
            else None,
        )
    for row in rows:
        for field in (
            "entry_cash_lamports",
            "closed_pnl_lamports",
            "failed_execution_cost_lamports",
            "last_liquidation_lamports",
            "exit_synthetic_shortfall_lamports",
            "exit_observed_real_sol_lamports",
            "exit_gross_curve_lamports",
            "sniper_trading_cash_cost_lamports",
            "sniper_creator_fee_income_lamports",
            "developer_observed_net_trade_cost_lamports",
            "developer_creator_fee_accrual_lamports",
            "developer_trade_cashflow_plus_fee_accrual_lamports",
            "token_total_creator_fee_accrual_lamports",
            "token_unallocated_creator_fee_accrual_lamports",
        ):
            if row.get(field) is not None:
                row[field.replace("lamports", "sol")] = sol_string(row[field])
    cash = next(
        (
            balance[3]
            for balance in result.final_balances
            if balance[0] == "portfolio:available:" + SOL.value and balance[2] == SOL.value
        ),
        0,
    )
    closed = [r for r in rows if r["status"] == "CLOSED"]
    opened = [r for r in rows if r["status"] == "OPEN"]
    failed_entry_cost = sum(
        r["failed_execution_cost_lamports"] for r in rows if not r["entry_cash_lamports"]
    )
    realized = sum(r["closed_pnl_lamports"] for r in closed) - failed_entry_cost
    open_cost = sum(r["entry_cash_lamports"] + r["failed_execution_cost_lamports"] for r in opened)
    if cash - policy.bankroll_lamports != realized - open_cost:
        raise ValueError("native ledger cash does not reconcile to closed PnL and open cost")
    if any(b[1] == "PORTFOLIO_RESERVED" and b[3] for b in result.final_balances):
        raise ValueError("native engine left a terminal order reservation")
    shortfall = sum(r.get("exit_synthetic_shortfall_lamports", 0) for r in closed)
    external_funding = -sum(
        b[3]
        for b in result.final_balances
        if b[1] == "EXTERNAL" and b[0].startswith("synthetic-liquidity:") and b[2] == SOL.value
    )
    if external_funding != shortfall:
        raise ValueError("synthetic liquidity evidence does not reconcile to the native ledger")
    summary = {
        "schema": "dnipro.sniper-window-result/v1",
        "execution_mode": execution_mode.value,
        "own_reserve_impact_propagated": execution.conditional is not None,
        "conditional_repricing_errors": len(execution.conditional.errors)
        if execution.conditional
        else 0,
        "conditional_repriced_events": len(execution.conditional.repriced)
        if execution.conditional
        else 0,
        "sell_liquidity_policy": execution.liquidity_policy.value,
        "synthetic_shortfall_lamports": shortfall,
        "synthetic_shortfall_sol": sol_string(shortfall),
        "synthetic_shortfall_positions": sum(
            r.get("exit_synthetic_shortfall_lamports", 0) > 0 for r in closed
        ),
        "synthetic_liquidity_reconciliation_verified": True,
        "policy": policy.document(),
        "creator_fee_income_method": "causal-event-recipient-vault-accrual-with-share-dust",
        "creator_fee_credit_enabled": bool(policy.creator_fee_credit_enabled),
        "observed_creator_fee_accrual_lamports": sum(strategy.creator_income.total.values()),
        "unallocated_creator_fee_accrual_lamports": sum(
            strategy.creator_income.unallocated.values()
        ),
        "creator_fee_allocation_conservation_verified": True,
        "creator_anchor_signals": sum(bool(r.get("sniper_is_launch_creator")) for r in rows),
        "fee_receiving_anchor_signals": sum(
            r.get("sniper_creator_fee_income_lamports", 0) > 0 for r in rows
        ),
        "implementation": identity,
        "source_manifest_sha256": source.manifest_hash,
        "source_input_sha256": source.source_hash,
        "inventory_base_source_sha256": getattr(source, "inventory_base_source_sha256", None),
        "inventory_enrichment_sha256": getattr(source, "inventory_enrichment_sha256", None),
        "inventory_transfers_provided": len(getattr(source, "inventory_transfers", ())),
        "inventory_transfers_applied": len(sink.inventory_transfers_applied),
        "empty_block_evidence_sha256": source.empty_block_evidence_hash,
        "proven_empty_slots": source.proven_empty_slots,
        "cohort_start_utc": source.input_policy["cohort_start_utc"],
        "cohort_end_utc": source.input_policy["cohort_end_utc"],
        "data_end_utc": source.input_policy.get("data_end_utc"),
        "cohort_launches": len(rows),
        "retained_input_trade_events": len(source.trade_rows),
        "engine_summary": asdict(result),
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "reason_counts": dict(Counter(r["reason"] for r in rows)),
        "signals": sum("signal_slot" in r for r in rows),
        "filled_entries": len(closed) + len(opened),
        "closed_positions": len(closed),
        "open_positions": len(opened),
        "realized_net_pnl_lamports": realized,
        "realized_net_pnl_sol": sol_string(realized),
        "closed_trade_pnl_lamports": sum(r["closed_pnl_lamports"] for r in closed),
        "failed_entry_cost_lamports": failed_entry_cost,
        "open_cash_cost_lamports": open_cost,
        "open_cash_cost_sol": sol_string(open_cost),
        "zero_recovery_stress_pnl_sol": sol_string(realized - open_cost),
        "final_available_sol": sol_string(cash),
        "cash_reconciliation_verified": True,
        "win_rate": format(
            Decimal(sum(r["closed_pnl_lamports"] > 0 for r in closed)) / len(closed), ".6f"
        )
        if closed
        else None,
        "publication_gaps": source.gaps,
        "exact_execution": False,
        "limitations": [
            "retained decoded subset; transfers and exact holdings unproven",
            "historical market prices stay unchanged by our orders"
            if execution.conditional is None
            else (
                "fixed observed counterpart intents; unknown slippage limits and trader reactions; "
                "conflicts retain open exposure"
            ),
            "historical fee-tier config not reconstructed",
            "network/rent are configured scenarios; tips zero",
            "within-slot landing rank is an assumption",
            "creator income is event-time vault accrual, not proven withdrawals or full-wallet PnL",
            *(
                [
                    "virtual settlement includes explicitly synthetic funding, "
                    "not proven on-chain liquidity"
                ]
                if execution_mode is ExecutionMode.EXOGENOUS_VIRTUAL_SETTLEMENT
                else []
            ),
        ],
    }
    return summary, rows, sink


def execute(args: Any) -> None:
    if args.require_exact:
        raise ValueError(
            "this source supports conditional historical replay, not exact execution admission"
        )
    if args.out.exists():
        raise ValueError("output exists; choose a fresh destination")
    source = SilverSniperSource(args.snapshot, args.empty_block_evidence)
    inventory_path = getattr(args, "inventory_transfers", None)
    if inventory_path is not None:
        if args.execution_mode != ExecutionMode.CONDITIONAL_PROTOCOL_REPLAY.value:
            raise ValueError("inventory overlay currently requires conditional protocol replay")
        from backtest.adapters.source.sniper_inventory import enrich_inventory

        enrich_inventory(source, inventory_path)
    policy = SniperWindowPolicy.load(source.input_policy)
    if args.policy is not None:
        policy = replace(policy, **json.loads(args.policy.read_text()))
    overrides = {
        "target_return_bps": args.target_return_bps,
        "holding_seconds": args.holding_seconds,
        "bankroll_lamports": args.initial_sol_lamports,
        "minimum_entry_age_seconds": args.minimum_entry_age_seconds,
        "maximum_entry_age_seconds": getattr(args, "maximum_entry_age_seconds", None),
    }
    policy = replace(policy, **{k: v for k, v in overrides.items() if v is not None})
    if args.without_creator_fee_credit:
        policy = replace(policy, creator_fee_credit_enabled=0)
    if args.delay_slots is not None:
        policy = replace(
            policy, entry_delay_slots=args.delay_slots, exit_delay_slots=args.delay_slots
        )
    print(
        f"Verified {len(source.births):,} launches and {len(source.trade_rows):,} retained trades; "
        "using native ReferenceBacktestEngine",
        flush=True,
    )
    summary, rows, sink = run(
        source, policy, ExecutionMode(args.execution_mode), trace_mints=set(args.trace_mint)
    )
    stage = args.out.with_name(args.out.name + ".partial-" + str(os.getpid()))
    stage.mkdir(parents=True, exist_ok=False)
    write_csv(stage / "token_results.csv", rows)
    write_csv(stage / "fills.csv", sink.fills)
    ledger_rows = [
        dict(
            transaction_id=tx["transaction_id"],
            correlation_id=tx["correlation_id"],
            boundary_ordinal=tx["boundary_ordinal"],
            reason=tx["reason"],
            **posting,
        )
        for tx in sink.ledger
        for posting in tx["postings"]
    ]
    write_csv(stage / "ledger.csv", ledger_rows)
    write_csv(stage / "execution_failures.csv", sink.failures)
    write_csv(stage / "creator_fee_recipients.csv", sink.creator_fee_recipients)
    write_csv(stage / "strategy_trace.csv", sink.strategy_trace)
    write_csv(stage / "order_audit.csv", sink.order_audit)
    write_csv(stage / "conditional_errors.csv", sink.conditional_errors)
    write_csv(stage / "conditional_repriced.csv", sink.conditional_repriced)
    write_csv(stage / "inventory_transfers_applied.csv", sink.inventory_transfers_applied)
    publish_result(
        stage / "result.json",
        canonical_json_bytes(
            {"summary": summary, "positions": rows, "ledger": sink.ledger, "fills": sink.fills}
        ),
    )
    publish_result(
        stage / "manifest.json",
        canonical_json_bytes(
            {
                "complete": True,
                "implementation": summary["implementation"],
                "source_manifest_sha256": source.manifest_hash,
                "source_input_sha256": source.source_hash,
                "empty_block_evidence_sha256": source.empty_block_evidence_hash,
                "files": {p.name: file_hash(p) for p in stage.iterdir() if p.is_file()},
            }
        ),
    )
    stage.rename(args.out)
    print(
        json.dumps(
            {
                k: v
                for k, v in summary.items()
                if k not in ("engine_summary", "implementation", "limitations", "policy")
            },
            indent=2,
        )
    )
    print("Committed native engine results to " + str(args.out.resolve()), flush=True)
