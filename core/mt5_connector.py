"""MT5 Connector — handles all communication with MetaTrader 5."""
import MetaTrader5 as mt5
from core.models import AccountInfo, SymbolSpread


class MT5Connector:
    """Manages connection to MetaTrader 5 terminal."""

    def __init__(self, config: dict):
        self.config = config.get("mt5", {})
        self._connected = False

    def connect(self) -> bool:
        """Initialize connection to MT5 terminal.

        Reuses an existing live connection instead of re-initializing on
        every request (mt5.initialize() can take seconds each time).
        """
        if self._connected and mt5.terminal_info() is not None:
            return True

        kwargs = {}
        if self.config.get("terminal_path"):
            kwargs["path"] = self.config["terminal_path"]
        if self.config.get("login"):
            kwargs["login"] = int(self.config["login"])
            kwargs["password"] = str(self.config.get("password", ""))
            kwargs["server"] = str(self.config.get("server", ""))

        if not mt5.initialize(**kwargs):
            error = mt5.last_error()
            print(f"MT5 initialization failed: {error}")
            return False

        self._connected = True
        return True

    def disconnect(self):
        """Shutdown MT5 connection."""
        if self._connected:
            mt5.shutdown()
            self._connected = False

    def is_connected(self) -> bool:
        """Check if MT5 is connected."""
        return self._connected and mt5.terminal_info() is not None

    def get_account_info(self) -> AccountInfo:
        """Get current account balance, equity, currency, leverage."""
        info = mt5.account_info()
        if info is None:
            raise ConnectionError(
                "Cannot get account info. Is MT5 running and logged in?"
            )
        return AccountInfo(
            balance=info.balance,
            equity=info.equity,
            currency=info.currency,
            leverage=info.leverage,
        )

    def get_symbol_spread(self, symbol: str) -> SymbolSpread:
        """Get live spread and pip info for a symbol.

        Queries the MT5 terminal for real-time bid/ask prices and calculates
        the spread, pip size, and pip value for the given symbol.
        """
        # Ensure symbol is available in Market Watch
        if not mt5.symbol_select(symbol, True):
            raise ValueError(
                f"Symbol '{symbol}' not found in MT5. "
                f"Check the symbol name matches your broker."
            )

        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)

        if tick is None or info is None:
            raise ValueError(f"Cannot get market data for '{symbol}'")

        digits = info.digits
        point = info.point  # Smallest price change

        # Determine pip size based on symbol digits
        # 5-digit forex (e.g., EURUSD 1.08015): pip = 0.0001 (4th decimal)
        # 3-digit forex (e.g., USDJPY 150.123): pip = 0.01
        # 2-digit (e.g., XAUUSD 2650.12): pip = 0.1 or 0.01 depending on broker
        # 1-digit (e.g., indices): pip = 1 point
        if digits == 5 or digits == 3:
            pip_size = point * 10  # pip = 10 points
        elif digits == 4 or digits == 2:
            pip_size = point  # pip = 1 point
        else:
            pip_size = point  # fallback: pip = 1 point

        spread_points = info.spread
        spread_pips = (tick.ask - tick.bid) / pip_size

        # Calculate pip value for 1 standard lot
        # trade_tick_value = profit in account currency for 1 point move on 1 lot
        pip_value = info.trade_tick_value * (pip_size / point)

        return SymbolSpread(
            symbol=symbol,
            bid=tick.bid,
            ask=tick.ask,
            spread_points=spread_points,
            spread_pips=round(spread_pips, 2),
            pip_size=pip_size,
            pip_value=pip_value,
            digits=digits,
            min_lot=info.volume_min,
            max_lot=info.volume_max,
            lot_step=info.volume_step,
        )

    def get_current_price(self, symbol: str) -> tuple:
        """Get current (bid, ask) prices for a symbol."""
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise ValueError(f"Cannot get price for '{symbol}'")
        return tick.bid, tick.ask

    def normalize_lot_size(self, symbol: str, lot_size: float) -> float:
        """Round lot size to the nearest valid step for the broker."""
        info = mt5.symbol_info(symbol)
        if info is None:
            return round(lot_size, 2)

        step = info.volume_step
        min_lot = info.volume_min
        max_lot = info.volume_max

        # Round to nearest step
        lot_size = round(round(lot_size / step) * step, 8)
        # Clamp to broker limits
        lot_size = max(min_lot, min(lot_size, max_lot))
        return lot_size

    def get_trade_outcome(self, ticket: int):
        """Check whether a trade we placed has finished.

        `ticket` is the order ticket returned when the trade was placed.
        In MT5 the resulting position's identifier equals that ticket,
        both for market orders and for pending orders that later fill.

        Returns:
            None  — still open (position running or pending order waiting)
            dict  — {"state": "CLOSED", "pnl": float, "exit_price": float}
                    or {"state": "CANCELLED", "reason": str}
        """
        ticket = int(ticket)

        # Still running?
        if mt5.positions_get(ticket=ticket):
            return None
        if mt5.orders_get(ticket=ticket):
            return None  # pending order not triggered yet

        deals = mt5.history_deals_get(position=ticket) or ()
        if deals:
            exits = [d for d in deals if d.entry in _EXIT_DEAL_ENTRIES]
            if not exits:
                return None  # position filled; closing deal not in history yet
            # Net P&L: profit + commission + swap + fees over every deal
            pnl = sum(
                d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
                for d in deals
            )
            last_exit = max(exits, key=lambda d: d.time_msc)
            return {
                "state": "CLOSED",
                "pnl": round(pnl, 2),
                "exit_price": last_exit.price,
            }

        # No deals at all: pending order that never filled?
        orders = mt5.history_orders_get(ticket=ticket) or ()
        for o in orders:
            if o.state in _DEAD_ORDER_STATES:
                return {
                    "state": "CANCELLED",
                    "reason": _DEAD_ORDER_STATES[o.state],
                }
        return None  # unknown yet (e.g. history not loaded) — check again later


# Deal entry types that close (part of) a position
_EXIT_DEAL_ENTRIES = {
    getattr(mt5, "DEAL_ENTRY_OUT", 1),
    getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
}

# Order states meaning a pending order died without filling
_DEAD_ORDER_STATES = {
    getattr(mt5, "ORDER_STATE_CANCELED", 2): "cancelled",
    getattr(mt5, "ORDER_STATE_REJECTED", 5): "rejected",
    getattr(mt5, "ORDER_STATE_EXPIRED", 6): "expired",
}
