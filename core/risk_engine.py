"""Risk & Spread Engine — the brain of the trade assistant.

Calculates spread-adjusted SL/TP, position sizing, and validates
that trades meet the configured risk:reward and spread thresholds.
"""
from dataclasses import dataclass, asdict
from core.models import SymbolSpread, AccountInfo


@dataclass
class AdjustedTrade:
    """Trade with spread-adjusted SL, TP, and calculated lot size."""

    # Identity
    symbol: str
    direction: str
    order_type: str

    # Original values from chart
    chart_entry: float
    chart_sl: float
    chart_tp: float
    chart_sl_pips: float
    chart_tp_pips: float
    chart_rr: float

    # Adjusted values (after spread absorption)
    real_entry: float
    adjusted_sl: float
    adjusted_tp: float
    actual_sl_pips: float
    actual_tp_pips: float
    actual_rr: float

    # Position sizing
    lot_size: float
    risk_amount: float
    risk_percent: float
    potential_profit: float

    # Spread info
    spread_pips: float
    spread_acceptable: bool
    spread_warning: str

    # Validation
    is_valid: bool
    rejection_reason: str

    def to_dict(self) -> dict:
        return asdict(self)


class RiskEngine:
    """Calculates position sizing and adjusts SL/TP for live broker spread.

    Core logic:
    1. Query the chart SL/TP distances (in pips)
    2. Subtract the live spread from the SL distance so your actual dollar
       risk matches what the chart intended
    3. Recalculate TP to maintain your target RR
    4. Size the position from the adjusted SL distance + your risk %
    """

    def __init__(self, config: dict):
        risk = config["risk"]
        spread = config["spread"]

        self.risk_pct = risk["risk_percent"] / 100
        self.target_rr = risk["target_rr"]
        self.min_rr = risk["min_rr"]
        self.max_lots = risk["max_lot_size"]
        self.max_risk = risk["max_risk_dollars"]

        self.max_spread = spread["max_spread_pips"]
        self.warn_spread = spread["spread_warn_pips"]
        self.adjust_sl = spread["adjust_sl_for_spread"]
        self.adjust_tp = spread["adjust_tp_for_spread"]

    # ── Public API ──────────────────────────────────────────────────

    def calculate(
        self,
        symbol: str,
        direction: str,
        order_type: str,
        entry: float,
        sl: float,
        tp: float,
        account: AccountInfo,
        spread_info: SymbolSpread,
    ) -> AdjustedTrade:
        """Calculate the full trade with spread adjustment.

        Args:
            symbol: Trading symbol (e.g., "EURUSD")
            direction: "BUY" or "SELL"
            order_type: "MARKET", "BUY_LIMIT", etc.
            entry: Entry price from the chart (bid price)
            sl: Stop loss from the chart
            tp: Take profit from the chart
            account: Current MT5 account info
            spread_info: Live spread data from MT5

        Returns:
            AdjustedTrade with all calculations and validation results.
        """
        pip_size = spread_info.pip_size
        pip_value = spread_info.pip_value
        spread_pips = spread_info.spread_pips

        # ── Step 1: Chart distances (what you drew on TradingView) ──
        chart_sl_pips = abs(entry - sl) / pip_size
        chart_tp_pips = abs(entry - tp) / pip_size
        chart_rr = (
            chart_tp_pips / chart_sl_pips if chart_sl_pips > 0 else 0
        )

        # ── Step 2: Absorb spread into SL distance ──
        # Subtract spread so your dollar risk stays = chart risk
        intended_sl_pips = chart_sl_pips
        if self.adjust_sl:
            intended_sl_pips = chart_sl_pips - spread_pips

        # ── Step 3: Validate spread ──
        spread_warning = ""

        if spread_pips > self.max_spread:
            return self._rejected(
                symbol, direction, order_type, entry, sl, tp,
                chart_sl_pips, chart_tp_pips, chart_rr, spread_pips,
                f"Spread ({spread_pips:.1f} pips) exceeds max "
                f"allowed ({self.max_spread} pips). Wait for lower spread.",
            )

        if intended_sl_pips <= 0:
            return self._rejected(
                symbol, direction, order_type, entry, sl, tp,
                chart_sl_pips, chart_tp_pips, chart_rr, spread_pips,
                f"Spread ({spread_pips:.1f} pips) >= SL distance "
                f"({chart_sl_pips:.1f} pips). Trade impossible.",
            )

        if spread_pips > self.warn_spread:
            spread_warning = (
                f"⚠️ High spread: {spread_pips:.1f} pips "
                f"(threshold: {self.warn_spread})"
            )

        if chart_sl_pips > 0 and spread_pips / chart_sl_pips > 0.3:
            pct = spread_pips / chart_sl_pips * 100
            warn = (
                f"⚠️ Spread consumes {pct:.0f}% of SL distance"
            )
            spread_warning = (
                f"{spread_warning}\n{warn}" if spread_warning else warn
            )

        # ── Step 4: Calculate adjusted prices ──
        if direction == "BUY":
            real_entry = spread_info.ask  # Buys execute at ask
            adjusted_sl = real_entry - (intended_sl_pips * pip_size)
            if self.adjust_tp:
                adjusted_tp = real_entry + (
                    intended_sl_pips * self.target_rr * pip_size
                )
            else:
                adjusted_tp = tp

        else:  # SELL
            real_entry = spread_info.bid  # Sells execute at bid
            adjusted_sl = real_entry + (intended_sl_pips * pip_size)
            if self.adjust_tp:
                adjusted_tp = real_entry - (
                    intended_sl_pips * self.target_rr * pip_size
                )
            else:
                adjusted_tp = tp

        # ── Step 5: Final distances after adjustment ──
        actual_sl_pips = abs(real_entry - adjusted_sl) / pip_size
        actual_tp_pips = abs(real_entry - adjusted_tp) / pip_size
        actual_rr = (
            actual_tp_pips / actual_sl_pips if actual_sl_pips > 0 else 0
        )

        # ── Step 6: Validate RR ──
        if actual_rr < self.min_rr:
            return self._rejected(
                symbol, direction, order_type, entry, sl, tp,
                chart_sl_pips, chart_tp_pips, chart_rr, spread_pips,
                f"Adjusted RR (1:{actual_rr:.2f}) below minimum "
                f"(1:{self.min_rr})",
            )

        # ── Step 7: Position sizing ──
        risk_amount = account.balance * self.risk_pct
        risk_amount = min(risk_amount, self.max_risk)  # Hard cap

        if actual_sl_pips > 0 and pip_value > 0:
            lot_size = risk_amount / (actual_sl_pips * pip_value)
        else:
            lot_size = 0.01

        # Round and clamp
        lot_size = round(min(lot_size, self.max_lots), 2)
        lot_size = max(lot_size, 0.01)

        potential_profit = lot_size * actual_tp_pips * pip_value

        # ── Build result ──
        digits = spread_info.digits
        return AdjustedTrade(
            symbol=symbol,
            direction=direction,
            order_type=order_type,
            # Chart values
            chart_entry=entry,
            chart_sl=sl,
            chart_tp=tp,
            chart_sl_pips=round(chart_sl_pips, 1),
            chart_tp_pips=round(chart_tp_pips, 1),
            chart_rr=round(chart_rr, 2),
            # Adjusted values
            real_entry=round(real_entry, digits),
            adjusted_sl=round(adjusted_sl, digits),
            adjusted_tp=round(adjusted_tp, digits),
            actual_sl_pips=round(actual_sl_pips, 1),
            actual_tp_pips=round(actual_tp_pips, 1),
            actual_rr=round(actual_rr, 2),
            # Sizing
            lot_size=lot_size,
            risk_amount=round(risk_amount, 2),
            risk_percent=round(self.risk_pct * 100, 2),
            potential_profit=round(potential_profit, 2),
            # Spread
            spread_pips=round(spread_pips, 1),
            spread_acceptable=True,
            spread_warning=spread_warning.strip(),
            # Validation
            is_valid=True,
            rejection_reason="",
        )

    def update_settings(
        self,
        risk_pct: float = None,
        target_rr: float = None,
        min_rr: float = None,
        max_lots: float = None,
        max_spread: float = None,
    ):
        """Update risk settings on the fly without restarting."""
        if risk_pct is not None:
            self.risk_pct = risk_pct / 100
        if target_rr is not None:
            self.target_rr = target_rr
        if min_rr is not None:
            self.min_rr = min_rr
        if max_lots is not None:
            self.max_lots = max_lots
        if max_spread is not None:
            self.max_spread = max_spread

    # ── Private helpers ─────────────────────────────────────────────

    def _rejected(
        self, symbol, direction, order_type, entry, sl, tp,
        chart_sl_pips, chart_tp_pips, chart_rr, spread_pips, reason,
    ) -> AdjustedTrade:
        """Create a rejected trade result with zero values."""
        return AdjustedTrade(
            symbol=symbol, direction=direction, order_type=order_type,
            chart_entry=entry, chart_sl=sl, chart_tp=tp,
            chart_sl_pips=round(chart_sl_pips, 1),
            chart_tp_pips=round(chart_tp_pips, 1),
            chart_rr=round(chart_rr, 2),
            real_entry=0, adjusted_sl=0, adjusted_tp=0,
            actual_sl_pips=0, actual_tp_pips=0, actual_rr=0,
            lot_size=0, risk_amount=0, risk_percent=0,
            potential_profit=0,
            spread_pips=round(spread_pips, 1),
            spread_acceptable=False,
            spread_warning=reason,
            is_valid=False,
            rejection_reason=reason,
        )
