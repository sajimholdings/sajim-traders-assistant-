"""Shared data models used across all core modules.

These are kept separate from MT5-specific code so that modules like
the risk engine and tests can import them without requiring the
MetaTrader5 package to be installed.
"""
from dataclasses import dataclass, asdict


@dataclass
class AccountInfo:
    """Current MT5 account state."""
    balance: float
    equity: float
    currency: str
    leverage: int


@dataclass
class SymbolSpread:
    """Live spread and pip data for a trading symbol."""
    symbol: str
    bid: float
    ask: float
    spread_points: int
    spread_pips: float
    pip_size: float
    pip_value: float  # Per standard lot (1.0)
    digits: int
    min_lot: float = 0.01
    max_lot: float = 100.0
    lot_step: float = 0.01
