"""Notion Trade Journal — automatically logs every trade to Notion."""
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from notion_client import Client

from core.risk_engine import AdjustedTrade
from core.trade_executor import TradeResult

NOTION_API = "https://api.notion.com/v1"
# File uploads are done with plain HTTP so they work regardless of which
# notion-client version is installed. 2022-06-28 supports file uploads.
NOTION_UPLOAD_VERSION = "2022-06-28"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # Notion single-part upload limit


class NotionJournal:
    """Logs trades to a Notion database automatically.

    Notion database columns (properties) — names must match exactly:
        Trade        — Title
        Trade Date   — Date
        Symbol       — Text
        Side         — Select  (Long, Short)
        Entry Price  — Number
        Exit Price   — Number   (filled when the trade closes)
        Quantity     — Number   (lot size)
        SL           — Number
        TP           — Number
        RR           — Number
        Risk %       — Number
        Risk Amount  — Number
        Spread       — Number
        Order Type   — Select
        Status       — Select  (Open, Closed, Cancelled)
        Result       — Select  (Win, Loss, BE — filled when closed)
        P&L          — Number   (filled when closed)
        Ticket       — Number
        Notes        — Text

    The chart screenshot is added inside the page body as an image.
    """

    def __init__(self, config: dict):
        notion_cfg = config.get("notion", {})
        self.enabled = notion_cfg.get("enabled", False)
        self.attach_screenshot = notion_cfg.get("attach_screenshot", True)

        self.api_key = os.getenv("NOTION_API_KEY")
        self.database_id = os.getenv("NOTION_DATABASE_ID")

        # Reason journaling is unavailable (shown in the UI), or ""
        self.setup_error = ""

        if self.enabled and self.api_key and self.database_id:
            self.client = Client(auth=self.api_key)
        else:
            self.client = None
            if self.enabled:
                self.setup_error = (
                    "Notion journal enabled but NOTION_API_KEY or "
                    "NOTION_DATABASE_ID is not set in .env"
                )
                print(f"⚠️  {self.setup_error}. Journaling will be skipped.")

    # ── Create a journal entry ──────────────────────────────────────

    def log_trade(
        self,
        trade: AdjustedTrade,
        result: TradeResult,
        screenshot_path: str = None,
    ) -> dict:
        """Log a completed trade to Notion.

        Returns:
            {"url": str, "page_id": str, "error": str}
            error is "" on full success. If the page was created but the
            screenshot failed, url/page_id are set AND error explains it.
        """
        out = {"url": "", "page_id": "", "error": ""}

        if not self.enabled:
            return out
        if self.client is None:
            out["error"] = self.setup_error
            return out

        trade_name = (
            f"{trade.symbol} {trade.direction} #{result.order_ticket}"
        )
        now = datetime.now(timezone.utc).isoformat()

        notes_parts = []
        if trade.spread_warning:
            notes_parts.append(trade.spread_warning)
        notes_parts.append(
            f"Chart entry: {trade.chart_entry} | "
            f"Chart SL: {trade.chart_sl} | "
            f"Chart TP: {trade.chart_tp}"
        )
        notes_parts.append(
            f"Chart RR: 1:{trade.chart_rr} → "
            f"Adjusted RR: 1:{trade.actual_rr}"
        )
        notes_text = "\n".join(notes_parts)

        properties = {
            "Trade": {"title": [{"text": {"content": trade_name}}]},
            "Trade Date": {"date": {"start": now}},
            "Symbol": {"rich_text": [{"text": {"content": trade.symbol}}]},
            "Side": {"select": {
                "name": "Long" if trade.direction == "BUY" else "Short"
            }},
            "Entry Price": {
                "number": result.execution_price or trade.real_entry
            },
            "SL": {"number": trade.adjusted_sl},
            "TP": {"number": trade.adjusted_tp},
            "RR": {"number": trade.actual_rr},
            "Quantity": {"number": trade.lot_size},
            "Risk %": {"number": trade.risk_percent},
            "Risk Amount": {"number": trade.risk_amount},
            "Spread": {"number": trade.spread_pips},
            "Order Type": {"select": {"name": trade.order_type}},
            "Status": {"select": {"name": "Open"}},
            "Ticket": {"number": result.order_ticket},
            "Notes": {
                "rich_text": [{"text": {"content": notes_text[:2000]}}]
            },
        }

        try:
            page = self.client.pages.create(
                parent={"database_id": self.database_id},
                properties=properties,
            )
        except Exception as e:
            out["error"] = f"Notion entry not created: {e}"
            print(f"⚠️  {out['error']}")
            return out

        out["url"] = page.get("url", "")
        out["page_id"] = page.get("id", "")

        # Attach the chart screenshot inside the page (separate step, so a
        # failed upload never loses the journal row itself)
        if self.attach_screenshot and screenshot_path:
            try:
                self._attach_screenshot(out["page_id"], screenshot_path)
            except Exception as e:
                out["error"] = (
                    f"Logged to Notion, but screenshot not attached: {e}"
                )
                print(f"⚠️  {out['error']}")

        return out

    # ── Connection check (for the UI) ───────────────────────────────

    def check_connection(self) -> dict:
        """Verify the key + database ID work.

        Returns:
            {"enabled", "connected", "title", "url", "error"}
        """
        info = {"enabled": self.enabled, "connected": False,
                "title": "", "url": "", "error": ""}
        if not self.enabled:
            info["error"] = "Disabled in config.yaml (notion.enabled)"
            return info
        if self.client is None:
            info["error"] = self.setup_error
            return info
        try:
            r = requests.get(
                f"{NOTION_API}/databases/{self.database_id}",
                headers=self._headers(), timeout=15,
            )
            _raise_for_notion(r, "open database")
            db = r.json()
            info["connected"] = True
            info["title"] = "".join(
                t.get("plain_text", "") for t in db.get("title", [])
            ) or "Notion journal"
            info["url"] = db.get("url", "")
        except Exception as e:
            info["error"] = str(e)
        return info

    # ── Close out a journal entry ───────────────────────────────────

    def update_trade_result(
        self,
        page_id: str,
        pnl: float,
        status: str,
        result: str = None,
        exit_price: float = None,
    ) -> str:
        """Update a trade entry when it closes.

        Args:
            page_id: The Notion page ID of the trade.
            pnl: Final profit/loss amount (None to leave empty).
            status: "Closed" or "Cancelled".
            result: "Win", "Loss" or "BE" (closed trades only).
            exit_price: Price the position closed at.

        Returns:
            "" on success, otherwise the error message.
        """
        if not self.enabled or self.client is None or not page_id:
            return ""

        properties = {"Status": {"select": {"name": status}}}
        if pnl is not None:
            properties["P&L"] = {"number": round(pnl, 2)}
        if result:
            properties["Result"] = {"select": {"name": result}}
        if exit_price is not None:
            properties["Exit Price"] = {"number": exit_price}

        try:
            self.client.pages.update(page_id=page_id, properties=properties)
            return ""
        except Exception as e:
            print(f"⚠️  Notion update failed: {e}")
            return str(e)

    # ── Helpers ─────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": NOTION_UPLOAD_VERSION,
        }

    def _attach_screenshot(self, page_id: str, screenshot_path: str):
        """Upload the screenshot to Notion and add it to the page as an image."""
        path = Path(screenshot_path)
        if not path.exists():
            raise FileNotFoundError(f"screenshot file missing: {path.name}")
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError("screenshot larger than Notion's 20 MB limit")

        content_type = mimetypes.guess_type(path.name)[0] or "image/png"

        # 1) Create the upload slot
        r = requests.post(
            f"{NOTION_API}/file_uploads",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"filename": path.name, "content_type": content_type},
            timeout=30,
        )
        _raise_for_notion(r, "create upload")
        upload_id = r.json()["id"]

        # 2) Send the file bytes
        with open(path, "rb") as f:
            r = requests.post(
                f"{NOTION_API}/file_uploads/{upload_id}/send",
                headers=self._headers(),
                files={"file": (path.name, f, content_type)},
                timeout=60,
            )
        _raise_for_notion(r, "send file")

        # 3) Append it to the page body as an image block
        r = requests.patch(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={
                "children": [{
                    "object": "block",
                    "type": "image",
                    "image": {
                        "type": "file_upload",
                        "file_upload": {"id": upload_id},
                    },
                }]
            },
            timeout=30,
        )
        _raise_for_notion(r, "add image to page")


def _raise_for_notion(resp, step: str):
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message", resp.text)
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"{step} failed ({resp.status_code}): {detail}")
