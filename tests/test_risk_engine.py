"""Unit tests for the Risk & Spread Engine.

Run with: python -m pytest tests/test_risk_engine.py -v
Or simply: python tests/test_risk_engine.py
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.risk_engine import RiskEngine, AdjustedTrade
from core.models import SymbolSpread, AccountInfo


def make_config(
    risk_pct=1.0, target_rr=3.0, min_rr=1.5,
    max_lots=10.0, max_risk=1000,
    max_spread=5.0, warn_spread=3.0,
    adjust_sl=True, adjust_tp=True,
):
    """Helper to build a config dict."""
    return {
        "risk": {
            "risk_percent": risk_pct,
            "target_rr": target_rr,
            "min_rr": min_rr,
            "max_lot_size": max_lots,
            "max_risk_dollars": max_risk,
        },
        "spread": {
            "max_spread_pips": max_spread,
            "spread_warn_pips": warn_spread,
            "adjust_sl_for_spread": adjust_sl,
            "adjust_tp_for_spread": adjust_tp,
        },
    }


def make_account(balance=10000.0):
    return AccountInfo(
        balance=balance, equity=balance, currency="USD", leverage=100,
    )


def make_spread(
    symbol="EURUSD", bid=1.08000, ask=1.08015,
    spread_pips=1.5, pip_size=0.0001, pip_value=10.0, digits=5,
):
    spread_points = int((ask - bid) / (pip_size / 10))
    return SymbolSpread(
        symbol=symbol, bid=bid, ask=ask,
        spread_points=spread_points,
        spread_pips=spread_pips,
        pip_size=pip_size,
        pip_value=pip_value,
        digits=digits,
        min_lot=0.01, max_lot=100.0, lot_step=0.01,
    )


# ── Test Cases ──

def test_buy_trade_basic():
    """BUY: 20 pip SL, 60 pip TP, 1.5 pip spread, 1:3 target RR."""
    engine = RiskEngine(make_config())
    account = make_account(10000)
    spread = make_spread()

    result = engine.calculate(
        symbol="EURUSD", direction="BUY", order_type="MARKET",
        entry=1.08000, sl=1.07800, tp=1.08600,
        account=account, spread_info=spread,
    )

    assert result.is_valid, f"Trade should be valid: {result.rejection_reason}"
    assert result.chart_sl_pips == 20.0
    assert result.chart_tp_pips == 60.0

    # After spread adjustment: SL absorbs 1.5 pips
    # Intended SL = 20 - 1.5 = 18.5 pips
    assert result.actual_sl_pips == 18.5
    # TP = 18.5 * 3.0 = 55.5 pips
    assert result.actual_tp_pips == 55.5
    assert result.actual_rr == 3.0

    # Lot size = $100 / (18.5 pips * $10) = 0.54
    assert result.lot_size == 0.54
    assert result.risk_amount == 100.0

    print(f"✅ BUY basic: SL {result.actual_sl_pips}p, "
          f"TP {result.actual_tp_pips}p, RR 1:{result.actual_rr}, "
          f"lots {result.lot_size}")


def test_sell_trade_basic():
    """SELL: 20 pip SL, 60 pip TP, 1.5 pip spread, 1:3 target RR."""
    engine = RiskEngine(make_config())
    account = make_account(10000)
    spread = make_spread()

    result = engine.calculate(
        symbol="EURUSD", direction="SELL", order_type="MARKET",
        entry=1.08000, sl=1.08200, tp=1.07400,
        account=account, spread_info=spread,
    )

    assert result.is_valid, f"Trade should be valid: {result.rejection_reason}"
    assert result.chart_sl_pips == 20.0
    assert result.actual_sl_pips == 18.5  # Absorbs 1.5 pip spread
    assert result.actual_rr == 3.0

    print(f"✅ SELL basic: SL {result.actual_sl_pips}p, "
          f"RR 1:{result.actual_rr}, lots {result.lot_size}")


def test_spread_exceeds_max():
    """Reject trade when spread > max_spread setting."""
    engine = RiskEngine(make_config(max_spread=3.0))
    account = make_account()
    spread = make_spread(
        bid=1.08000, ask=1.08050,
        spread_pips=5.0,
    )

    result = engine.calculate(
        symbol="EURUSD", direction="BUY", order_type="MARKET",
        entry=1.08000, sl=1.07800, tp=1.08600,
        account=account, spread_info=spread,
    )

    assert not result.is_valid
    assert "exceeds max" in result.rejection_reason.lower()
    print(f"✅ Spread rejection: {result.rejection_reason}")


def test_spread_consumes_sl():
    """Reject when spread >= SL distance (impossible trade)."""
    engine = RiskEngine(make_config(max_spread=30.0))
    account = make_account()
    spread = make_spread(
        bid=1.08000, ask=1.08025,
        spread_pips=25.0,  # 25 pip spread
    )

    result = engine.calculate(
        symbol="EURUSD", direction="BUY", order_type="MARKET",
        entry=1.08000, sl=1.07980, tp=1.08600,  # only 2 pip SL
        account=account, spread_info=spread,
    )

    assert not result.is_valid
    assert "impossible" in result.rejection_reason.lower()
    print(f"✅ Spread > SL rejection: {result.rejection_reason}")


def test_rr_below_minimum():
    """Reject when adjusted RR falls below min_rr."""
    engine = RiskEngine(make_config(min_rr=2.0, target_rr=1.0))
    account = make_account()
    spread = make_spread()

    result = engine.calculate(
        symbol="EURUSD", direction="BUY", order_type="MARKET",
        entry=1.08000, sl=1.07800, tp=1.08200,  # 1:1 chart RR
        account=account, spread_info=spread,
    )

    assert not result.is_valid
    assert "below minimum" in result.rejection_reason.lower()
    print(f"✅ RR rejection: {result.rejection_reason}")


def test_risk_dollar_cap():
    """Risk amount should be capped at max_risk_dollars."""
    engine = RiskEngine(make_config(risk_pct=5.0, max_risk=200))
    account = make_account(100000)  # 5% = $5000, but cap at $200
    spread = make_spread()

    result = engine.calculate(
        symbol="EURUSD", direction="BUY", order_type="MARKET",
        entry=1.08000, sl=1.07800, tp=1.08600,
        account=account, spread_info=spread,
    )

    assert result.is_valid
    assert result.risk_amount == 200.0
    print(f"✅ Risk cap: risk_amount=${result.risk_amount} (capped)")


def test_update_settings():
    """update_settings() should change engine behavior."""
    engine = RiskEngine(make_config(risk_pct=1.0))
    engine.update_settings(risk_pct=2.0, target_rr=2.0)

    assert engine.risk_pct == 0.02
    assert engine.target_rr == 2.0
    print("✅ Settings update works")


def test_gold_trade():
    """Test with XAUUSD (Gold) — different pip size and value."""
    engine = RiskEngine(make_config(risk_pct=1.0, target_rr=2.0))
    account = make_account(5000)

    # Gold: 2 digits, pip_size = 0.1, typical spread ~3 pips
    spread = make_spread(
        symbol="XAUUSD", bid=2650.00, ask=2650.30,
        spread_pips=3.0, pip_size=0.1, pip_value=10.0, digits=2,
    )

    result = engine.calculate(
        symbol="XAUUSD", direction="BUY", order_type="MARKET",
        entry=2650.00, sl=2645.00, tp=2660.00,  # 50 pip SL, 100 pip TP
        account=account, spread_info=spread,
    )

    assert result.is_valid
    # Intended SL = 50 - 3 = 47 pips
    assert result.actual_sl_pips == 47.0
    print(f"✅ Gold trade: SL {result.actual_sl_pips}p, "
          f"RR 1:{result.actual_rr}, lots {result.lot_size}")


# ── Run all tests ──

if __name__ == "__main__":
    tests = [
        test_buy_trade_basic,
        test_sell_trade_basic,
        test_spread_exceeds_max,
        test_spread_consumes_sl,
        test_rr_below_minimum,
        test_risk_dollar_cap,
        test_update_settings,
        test_gold_trade,
    ]

    passed = 0
    failed = 0

    print("\n" + "=" * 50)
    print("  Risk Engine Unit Tests")
    print("=" * 50 + "\n")

    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except AssertionError as e:
            print(f"❌ {test_fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"💥 {test_fn.__name__}: {e}")
            failed += 1

    print(f"\n{'=' * 50}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'=' * 50}\n")

    if failed:
        sys.exit(1)
