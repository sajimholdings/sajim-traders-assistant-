"""Local CSV trade log — a permanent record that doesn't depend on Notion.

Every event is appended as one row to logs/trades.csv:

    ANALYZED            screenshot analyzed, trade passed the risk engine
    REJECTED_BY_ENGINE  analyzed, but the risk engine blocked it (reason in message)
    ANALYSIS_ERROR      AI / MT5 / extraction failed during analysis
    SKIPPED             you clicked Cancel on an analyzed trade
    EXECUTED            order accepted by the broker
    EXECUTION_FAILED    order rejected by the broker / MT5 error
    CLOSED              position closed — pnl, exit_price and result filled in
    CANCELLED           pending order cancelled / expired before filling

Rows are linked by analysis_id (same id as the screenshot file name) and,
once executed, by the MT5 ticket. The file opens directly in Excel.
"""
import csv
import threading
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_PATH = LOG_DIR / "trades.csv"

FIELDS = [
    "timestamp",
    "event",
    "analysis_id",
    "symbol",
    "direction",
    "order_type",
    "chart_entry",
    "chart_sl",
    "chart_tp",
    "entry",
    "sl",
    "tp",
    "rr",
    "lot_size",
    "risk_amount",
    "risk_percent",
    "spread_pips",
    "ai_confidence",
    "ticket",
    "exit_price",
    "pnl",
    "result",
    "message",
    "notion_page_id",
    "notion_url",
    "screenshot",
]

# Events after which a ticket is no longer tracked as open
_FINAL_EVENTS = {"CLOSED", "CANCELLED"}


class TradeLog:
    """Append-only CSV log, safe to call from multiple Flask threads."""

    def __init__(self, path: Path = LOG_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()

    # ── Writing ─────────────────────────────────────────────────────

    def log(self, event: str, trade: dict = None, **extra) -> bool:
        """Append one event row. Never raises — logging must not block trading.

        Args:
            event: One of the event names listed in the module docstring.
            trade: Optional AdjustedTrade dict; its fields are mapped to columns.
            **extra: Any other column values (ticket, pnl, message, ...).

        Returns:
            True if the row was written.
        """
        row = {"timestamp": datetime.now().isoformat(timespec="seconds"),
               "event": event}
        if trade:
            row.update(_trade_columns(trade))
        row.update({k: v for k, v in extra.items() if v is not None})

        # Keep each row on one line in Excel
        if "message" in row:
            row["message"] = str(row["message"]).replace("\n", " | ")

        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                new_file = not self.path.exists()
                # utf-8-sig BOM on a new file so Excel shows emojis/symbols
                with open(
                    self.path, "a", newline="",
                    encoding="utf-8-sig" if new_file else "utf-8",
                ) as f:
                    writer = csv.DictWriter(
                        f, fieldnames=FIELDS, extrasaction="ignore"
                    )
                    if new_file:
                        writer.writeheader()
                    writer.writerow(row)
            return True
        except PermissionError:
            print(
                f"⚠️  Could not write to {self.path} — is it open in Excel? "
                f"Close it so new trades can be logged. (lost event: {event})"
            )
        except Exception as e:
            print(f"⚠️  Local trade log failed ({event}): {e}")
        return False

    # ── Reading ─────────────────────────────────────────────────────

    def read_all(self) -> list:
        """Return all rows as dicts (empty list if no log yet)."""
        if not self.path.exists():
            return []
        try:
            with self._lock:
                with open(self.path, newline="", encoding="utf-8-sig") as f:
                    return list(csv.DictReader(f))
        except Exception as e:
            print(f"⚠️  Could not read local trade log: {e}")
            return []

    def summary(self) -> dict:
        """Counts for the UI."""
        rows = self.read_all()
        events = [r.get("event") for r in rows]
        return {
            "path": str(self.path.resolve()),
            "exists": self.path.exists(),
            "entries": len(rows),
            "analyzed": sum(
                e in ("ANALYZED", "REJECTED_BY_ENGINE") for e in events
            ),
            "executed": events.count("EXECUTED"),
            "closed": events.count("CLOSED"),
            "open": len(self.open_trades_from(rows)),
        }

    def open_trades(self) -> dict:
        return self.open_trades_from(self.read_all())

    @staticmethod
    def open_trades_from(rows: list) -> dict:
        """Tickets that were executed but not yet closed/cancelled.

        Returns:
            {ticket (int): the EXECUTED row (dict)}
        """
        open_by_ticket = {}
        for row in rows:
            ticket = _to_int(row.get("ticket"))
            if not ticket:
                continue
            if row.get("event") == "EXECUTED":
                open_by_ticket[ticket] = row
            elif row.get("event") in _FINAL_EVENTS:
                open_by_ticket.pop(ticket, None)
        return open_by_ticket


def _trade_columns(t: dict) -> dict:
    """Map an AdjustedTrade dict onto the CSV columns."""
    return {
        "symbol": t.get("symbol"),
        "direction": t.get("direction"),
        "order_type": t.get("order_type"),
        "chart_entry": t.get("chart_entry"),
        "chart_sl": t.get("chart_sl"),
        "chart_tp": t.get("chart_tp"),
        "entry": t.get("real_entry"),
        "sl": t.get("adjusted_sl"),
        "tp": t.get("adjusted_tp"),
        "rr": t.get("actual_rr"),
        "lot_size": t.get("lot_size"),
        "risk_amount": t.get("risk_amount"),
        "risk_percent": t.get("risk_percent"),
        "spread_pips": t.get("spread_pips"),
        "message": t.get("rejection_reason") or t.get("spread_warning") or "",
    }


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
