"""Flask API routes — connects the web UI to the trading engine."""
import os
import threading
import time
import uuid
from pathlib import Path
from flask import (
    Blueprint, request, jsonify, render_template, send_file,
)
from dataclasses import asdict

from core import load_config, save_config
from core.mt5_connector import MT5Connector
from core.vision_extractor import VisionExtractor
from core.risk_engine import RiskEngine, AdjustedTrade
from core.trade_executor import TradeExecutor
from core.notion_journal import NotionJournal
from core.trade_log import TradeLog

api = Blueprint("api", __name__)

UPLOAD_DIR = Path(__file__).parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Local CSV log (logs/trades.csv) — always on, independent of Notion
trade_log = TradeLog()

# Only one close-out sync at a time (status polling + manual sync)
_sync_lock = threading.Lock()

# ── Component singletons (initialized on first request) ──
_components = {}


def _get_components():
    """Lazy-initialize all components on first use."""
    if not _components:
        config = load_config()
        api_key = os.getenv("GEMINI_API_KEY", "")

        _components["config"] = config
        _components["mt5"] = MT5Connector(config)
        vision_cfg = config.get("vision", {}) or {}
        _components["extractor"] = VisionExtractor(
            api_key,
            model=vision_cfg.get("model", "gemini-3.6-flash"),
            thinking_level=vision_cfg.get("thinking_level", "low"),
            fallback_models=vision_cfg.get("fallback_models", []),
            timeout_seconds=vision_cfg.get("timeout_seconds", 20),
        )
        _components["risk_engine"] = RiskEngine(config)
        _components["executor"] = TradeExecutor(config)
        _components["journal"] = NotionJournal(config)

    return _components


# ── Pages ───────────────────────────────────────────────────────────

@api.route("/")
def index():
    """Serve the main drag & drop UI."""
    return render_template("index.html")


# ── API Endpoints ───────────────────────────────────────────────────

@api.route("/api/analyze", methods=["POST"])
def analyze_screenshot():
    """Upload a screenshot and get the full trade analysis.

    Expects multipart form data with a 'screenshot' file.
    Returns the extracted trade data, adjusted calculations,
    and account info. Every outcome is written to the local CSV log.
    """
    if "screenshot" not in request.files:
        return jsonify({"error": "No screenshot uploaded"}), 400

    file = request.files["screenshot"]
    if not file.filename:
        return jsonify({"error": "Empty file"}), 400

    file_bytes = file.read()
    mime_type = file.content_type or "image/png"

    # Save screenshot for journaling; its id links all log rows
    analysis_id = uuid.uuid4().hex
    ext = Path(file.filename).suffix or ".png"
    save_path = UPLOAD_DIR / f"{analysis_id}{ext}"
    save_path.write_bytes(file_bytes)

    comp = _get_components()
    t0 = time.perf_counter()
    extracted = None

    def log_error(message: str):
        trade_log.log(
            "ANALYSIS_ERROR",
            analysis_id=analysis_id,
            symbol=getattr(extracted, "symbol", None),
            direction=getattr(extracted, "direction", None),
            order_type=getattr(extracted, "order_type", None),
            chart_entry=getattr(extracted, "entry_price", None),
            chart_sl=getattr(extracted, "stop_loss", None),
            chart_tp=getattr(extracted, "take_profit", None),
            ai_confidence=getattr(extracted, "confidence", None),
            message=message,
            screenshot=str(save_path),
        )

    try:
        # Step 1: Extract trade from screenshot using Gemini Vision
        extracted = comp["extractor"].extract_from_bytes(
            file_bytes, mime_type
        )
        t_ai = time.perf_counter()

        if not extracted.symbol or not extracted.entry_price:
            msg = (
                "Could not extract trade setup from screenshot. "
                "Make sure the entry, SL, and TP are clearly visible "
                "on the chart."
            )
            log_error(msg)
            return jsonify({"error": msg}), 400

        # Step 2: Connect to MT5 and get live data
        mt5_conn = comp["mt5"]
        if not mt5_conn.connect():
            msg = (
                "Cannot connect to MT5. Make sure MetaTrader 5 is "
                "running and logged in."
            )
            log_error(msg)
            return jsonify({"error": msg}), 500

        account = mt5_conn.get_account_info()
        spread_info = mt5_conn.get_symbol_spread(extracted.symbol)

        # Step 3: Calculate adjusted trade
        adjusted = comp["risk_engine"].calculate(
            symbol=extracted.symbol,
            direction=extracted.direction,
            order_type=extracted.order_type,
            entry=extracted.entry_price,
            sl=extracted.stop_loss,
            tp=extracted.take_profit,
            account=account,
            spread_info=spread_info,
        )

        # Normalize lot size to broker constraints
        if adjusted.is_valid:
            adjusted.lot_size = mt5_conn.normalize_lot_size(
                adjusted.symbol, adjusted.lot_size
            )

        t_end = time.perf_counter()
        print(
            f"⏱️  analyze: Gemini {t_ai - t0:.1f}s | "
            f"MT5+risk {t_end - t_ai:.1f}s | total {t_end - t0:.1f}s"
        )

        trade_log.log(
            "ANALYZED" if adjusted.is_valid else "REJECTED_BY_ENGINE",
            trade=adjusted.to_dict(),
            analysis_id=analysis_id,
            ai_confidence=extracted.confidence,
            screenshot=str(save_path),
        )

        return jsonify({
            "analysis_id": analysis_id,
            "extracted": asdict(extracted),
            "adjusted": adjusted.to_dict(),
            "account": asdict(account),
            "screenshot_path": str(save_path),
        })

    except Exception as e:
        log_error(str(e))
        return jsonify({"error": str(e)}), 500


@api.route("/api/execute", methods=["POST"])
def execute_trade():
    """Confirm and execute the analyzed trade on MT5.

    Expects JSON body with 'adjusted' (the AdjustedTrade dict),
    and optionally 'analysis_id' and 'screenshot_path'.
    """
    data = request.json
    if not data or "adjusted" not in data:
        return jsonify({"error": "No trade data provided"}), 400

    comp = _get_components()
    analysis_id = data.get("analysis_id")
    screenshot_path = data.get("screenshot_path")
    confidence = (data.get("extracted") or {}).get("confidence")

    try:
        # Rebuild the AdjustedTrade from the confirmation data
        adjusted = AdjustedTrade(**data["adjusted"])

        # Ensure MT5 is still connected
        mt5_conn = comp["mt5"]
        if not mt5_conn.connect():
            msg = "MT5 disconnected. Reconnect and try again."
            trade_log.log(
                "EXECUTION_FAILED", trade=data["adjusted"],
                analysis_id=analysis_id, message=msg,
                screenshot=screenshot_path,
            )
            return jsonify({"error": msg}), 500

        # Execute on MT5
        result = comp["executor"].execute(adjusted)

        if not result.success:
            trade_log.log(
                "EXECUTION_FAILED", trade=data["adjusted"],
                analysis_id=analysis_id, ai_confidence=confidence,
                message=result.message, screenshot=screenshot_path,
            )
            return jsonify({
                "success": False,
                "message": result.message,
                "ticket": 0,
                "execution_price": 0,
                "notion_url": "",
                "notion_error": "",
            })

        # Journal to Notion (never blocks — errors are reported, not raised)
        notion = comp["journal"].log_trade(
            adjusted, result, screenshot_path=screenshot_path,
        )

        # Local log — always written, holds the Notion page id for close-out
        logged = trade_log.log(
            "EXECUTED", trade=data["adjusted"],
            analysis_id=analysis_id,
            ai_confidence=confidence,
            entry=result.execution_price or adjusted.real_entry,
            ticket=result.order_ticket,
            # None keeps any spread warning from the trade in the message
            message=(
                f"Notion: {notion['error']}" if notion["error"] else None
            ),
            notion_page_id=notion["page_id"],
            notion_url=notion["url"],
            screenshot=screenshot_path,
        )

        return jsonify({
            "success": True,
            "message": result.message,
            "ticket": result.order_ticket,
            "execution_price": result.execution_price,
            "notion_url": notion["url"],
            "notion_error": notion["error"],
            "local_logged": logged,
        })

    except Exception as e:
        trade_log.log(
            "EXECUTION_FAILED", trade=data.get("adjusted"),
            analysis_id=analysis_id, message=str(e),
            screenshot=screenshot_path,
        )
        return jsonify({"error": str(e)}), 500


@api.route("/api/skip", methods=["POST"])
def skip_trade():
    """Record that an analyzed trade was cancelled by the user."""
    data = request.json or {}
    trade_log.log(
        "SKIPPED",
        trade=data.get("adjusted"),
        analysis_id=data.get("analysis_id"),
        ai_confidence=(data.get("extracted") or {}).get("confidence"),
        screenshot=data.get("screenshot_path"),
        message="Cancelled by user",
    )
    return jsonify({"status": "logged"})


@api.route("/api/sync", methods=["POST"])
def sync_trades():
    """Manually check MT5 for closed trades and update the logs."""
    comp = _get_components()
    if not comp["mt5"].connect():
        return jsonify({"error": "MT5 is not running or not logged in"}), 500
    return jsonify({"closed_trades": _sync_closed_trades(comp)})


def _sync_closed_trades(comp) -> list:
    """Find our open trades that have closed in MT5 and record the result.

    Writes a CLOSED/CANCELLED row to the CSV and updates P&L + Status in
    Notion. Safe to call often: trades already closed are skipped.

    Returns:
        List of short summaries of trades that closed during this call.
    """
    if not _sync_lock.acquire(blocking=False):
        return []  # another sync is already running

    closed = []
    try:
        mt5_conn = comp["mt5"]
        journal = comp["journal"]

        for ticket, row in trade_log.open_trades().items():
            try:
                outcome = mt5_conn.get_trade_outcome(ticket)
            except Exception as e:
                print(f"⚠️  Could not check ticket #{ticket}: {e}")
                continue
            if outcome is None:
                continue

            common = dict(
                analysis_id=row.get("analysis_id"),
                symbol=row.get("symbol"),
                direction=row.get("direction"),
                order_type=row.get("order_type"),
                lot_size=row.get("lot_size"),
                ticket=ticket,
                notion_page_id=row.get("notion_page_id"),
                notion_url=row.get("notion_url"),
            )

            if outcome["state"] == "CLOSED":
                pnl = outcome["pnl"]
                if pnl > 0:
                    result = "Win"
                elif pnl < 0:
                    result = "Loss"
                else:
                    result = "BE"
                notion_err = journal.update_trade_result(
                    row.get("notion_page_id"), pnl, "Closed",
                    result=result, exit_price=outcome["exit_price"],
                )
                trade_log.log(
                    "CLOSED", **common,
                    entry=row.get("entry"),
                    exit_price=outcome["exit_price"],
                    pnl=pnl, result=result,
                    message=f"Notion: {notion_err}" if notion_err else "",
                )
                summary = (
                    f"{row.get('symbol')} #{ticket} closed: "
                    f"{pnl:+.2f} ({result})"
                )
            else:  # CANCELLED pending order
                notion_err = journal.update_trade_result(
                    row.get("notion_page_id"), None, "Cancelled",
                )
                trade_log.log(
                    "CANCELLED", **common, result="Cancelled",
                    message=(
                        f"Pending order {outcome['reason']}"
                        + (f" | Notion: {notion_err}" if notion_err else "")
                    ),
                )
                summary = (
                    f"{row.get('symbol')} #{ticket} pending order "
                    f"{outcome['reason']}"
                )

            print(f"📕  {summary}")
            closed.append(summary)
    finally:
        _sync_lock.release()

    return closed


# ── Journal (Notion + local CSV) ────────────────────────────────────

_notion_status_cache = {"at": 0.0, "data": None}


@api.route("/api/journal/status", methods=["GET"])
def journal_status():
    """Where trades are being logged, and whether each target works.

    The Notion check is cached for 60s (add ?refresh=1 to force).
    """
    comp = _get_components()
    now = time.time()
    cached = _notion_status_cache
    if (
        request.args.get("refresh")
        or cached["data"] is None
        or now - cached["at"] > 60
    ):
        cached["data"] = comp["journal"].check_connection()
        cached["at"] = now

    return jsonify({
        "notion": cached["data"],
        "csv": trade_log.summary(),
    })


@api.route("/api/journal/csv", methods=["GET"])
def journal_csv_download():
    """Download the local trade log."""
    if not trade_log.path.exists():
        return jsonify({"error": "No trades logged yet"}), 404
    return send_file(
        trade_log.path.resolve(),
        mimetype="text/csv",
        as_attachment=True,
        download_name="trades.csv",
    )


@api.route("/journal")
def journal_page():
    """Read-only view of the local CSV log, newest first."""
    from core.trade_log import FIELDS
    rows = list(reversed(trade_log.read_all()))
    return render_template(
        "journal.html",
        rows=rows,
        fields=FIELDS,
        path=str(trade_log.path.resolve()),
    )


@api.route("/api/settings", methods=["GET"])
def get_settings():
    """Get current risk/spread/trade settings."""
    return jsonify(load_config())


@api.route("/api/settings", methods=["POST"])
def update_settings():
    """Update settings on the fly without restarting.

    Expects JSON body with nested keys matching config.yaml structure.
    Example: {"risk": {"risk_percent": 2.0, "target_rr": 2.5}}
    """
    new_settings = request.json
    if not new_settings:
        return jsonify({"error": "No settings provided"}), 400

    config = load_config()

    # Merge updates into existing config
    for section, values in new_settings.items():
        if section in config and isinstance(values, dict):
            config[section].update(values)

    save_config(config)

    # Reload the risk engine with new settings
    comp = _get_components()
    comp["risk_engine"] = RiskEngine(config)
    comp["config"] = config

    return jsonify({"status": "Settings updated", "config": config})


@api.route("/api/mt5/status", methods=["GET"])
def mt5_status():
    """Check MT5 connection status and account info.

    Polled by the UI every 30s — also used to close out finished trades.
    """
    comp = _get_components()
    mt5_conn = comp["mt5"]

    try:
        if not mt5_conn.connect():
            return jsonify({
                "connected": False,
                "message": "MT5 is not running or not logged in",
            })

        account = mt5_conn.get_account_info()

        try:
            closed_trades = _sync_closed_trades(comp)
        except Exception as e:
            print(f"⚠️  Trade close-out sync failed: {e}")
            closed_trades = []

        return jsonify({
            "connected": True,
            "balance": account.balance,
            "equity": account.equity,
            "currency": account.currency,
            "leverage": account.leverage,
            "closed_trades": closed_trades,
        })
    except Exception as e:
        return jsonify({
            "connected": False,
            "message": str(e),
        })
