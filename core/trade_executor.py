"""Trade Executor — places orders on MetaTrader 5."""
import MetaTrader5 as mt5
from dataclasses import dataclass
from core.risk_engine import AdjustedTrade

# The MetaTrader5 Python package does NOT export the SYMBOL_FILLING_* flags
# (they only exist in MQL5), so reading them as mt5.SYMBOL_FILLING_FOK crashes.
# These are the documented bit values of symbol_info().filling_mode.
SYMBOL_FILLING_FOK = getattr(mt5, "SYMBOL_FILLING_FOK", 1)
SYMBOL_FILLING_IOC = getattr(mt5, "SYMBOL_FILLING_IOC", 2)

# Retcode the broker returns when the filling type is not supported
TRADE_RETCODE_INVALID_FILL = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)


@dataclass
class TradeResult:
    """Result of an MT5 order execution."""
    success: bool
    order_ticket: int
    message: str
    execution_price: float


class TradeExecutor:
    """Executes trades on MetaTrader 5 using the adjusted trade data."""

    # Map (direction, order_type) → MT5 order type constant
    ORDER_TYPE_MAP = {
        ("BUY", "MARKET"): mt5.ORDER_TYPE_BUY,
        ("SELL", "MARKET"): mt5.ORDER_TYPE_SELL,
        ("BUY", "BUY_LIMIT"): mt5.ORDER_TYPE_BUY_LIMIT,
        ("SELL", "SELL_LIMIT"): mt5.ORDER_TYPE_SELL_LIMIT,
        ("BUY", "BUY_STOP"): mt5.ORDER_TYPE_BUY_STOP,
        ("SELL", "SELL_STOP"): mt5.ORDER_TYPE_SELL_STOP,
    }

    def __init__(self, config: dict):
        trade_cfg = config.get("trade", {})
        self.magic = trade_cfg.get("magic_number", 234000)
        self.deviation = trade_cfg.get("deviation", 20)
        self.comment = trade_cfg.get("comment", "AI-Assistant")

    def execute(self, trade: AdjustedTrade) -> TradeResult:
        """Place a trade on MT5 based on the adjusted trade calculation.

        Args:
            trade: A fully calculated AdjustedTrade from the RiskEngine.

        Returns:
            TradeResult with success/failure status and order ticket.
        """
        if not trade.is_valid:
            return TradeResult(
                success=False,
                order_ticket=0,
                message=f"Trade rejected: {trade.rejection_reason}",
                execution_price=0,
            )

        # Resolve MT5 order type
        order_type_key = (trade.direction, trade.order_type)
        mt5_order_type = self.ORDER_TYPE_MAP.get(order_type_key)

        if mt5_order_type is None:
            return TradeResult(
                success=False,
                order_ticket=0,
                message=(
                    f"Unknown order type combination: "
                    f"{trade.direction} / {trade.order_type}"
                ),
                execution_price=0,
            )

        # Determine the correct fill type for the broker
        filling_type = self._get_filling_type(trade.symbol)

        # Build the order request
        is_market = trade.order_type == "MARKET"

        # Market orders fill at the live price, not the price captured at
        # analysis time (which may be seconds/minutes old -> requotes).
        price = trade.real_entry
        if is_market:
            tick = mt5.symbol_info_tick(trade.symbol)
            if tick is not None:
                price = tick.ask if trade.direction == "BUY" else tick.bid
        request = {
            "action": (
                mt5.TRADE_ACTION_DEAL if is_market
                else mt5.TRADE_ACTION_PENDING
            ),
            "symbol": trade.symbol,
            "volume": trade.lot_size,
            "type": mt5_order_type,
            "price": price,
            "sl": trade.adjusted_sl,
            "tp": trade.adjusted_tp,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": self.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_type,
        }

        # Send the order to MT5. If the broker rejects the filling type,
        # retry with the other modes (a rejected order is never placed,
        # so retrying cannot create duplicate positions).
        result = mt5.order_send(request)
        if (
            result is not None
            and result.retcode == TRADE_RETCODE_INVALID_FILL
        ):
            for alt in (
                mt5.ORDER_FILLING_FOK,
                mt5.ORDER_FILLING_IOC,
                mt5.ORDER_FILLING_RETURN,
            ):
                if alt == filling_type:
                    continue
                request["type_filling"] = alt
                result = mt5.order_send(request)
                if (
                    result is None
                    or result.retcode != TRADE_RETCODE_INVALID_FILL
                ):
                    break

        if result is None:
            error = mt5.last_error()
            return TradeResult(
                success=False,
                order_ticket=0,
                message=f"Order send failed: {error}",
                execution_price=0,
            )

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return TradeResult(
                success=False,
                order_ticket=0,
                message=(
                    f"Broker rejected order: {result.comment} "
                    f"(code: {result.retcode})"
                ),
                execution_price=0,
            )

        return TradeResult(
            success=True,
            order_ticket=result.order,
            message=f"✅ Order placed: #{result.order}",
            execution_price=result.price,
        )

    def _get_filling_type(self, symbol: str) -> int:
        """Determine the correct filling type supported by the broker.

        Different brokers support different fill policies. This queries
        the symbol info to find the right one.
        """
        info = mt5.symbol_info(symbol)
        if info is None:
            return mt5.ORDER_FILLING_IOC  # fallback

        filling = info.filling_mode

        if filling & SYMBOL_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        elif filling & SYMBOL_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        else:
            return mt5.ORDER_FILLING_RETURN
