"""Gemini Vision Trade Extractor — reads TradingView screenshots with AI."""
import json
import time
import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from pathlib import Path
from dataclasses import dataclass, asdict
from google import genai


@dataclass
class ExtractedTrade:
    """Trade setup data extracted from a screenshot by Gemini Vision."""
    symbol: str          # e.g., "EURUSD", "XAUUSD"
    direction: str       # "BUY" or "SELL"
    entry_price: float   # Entry level from chart
    stop_loss: float     # SL level from chart
    take_profit: float   # TP level from chart
    order_type: str      # "MARKET", "BUY_LIMIT", "SELL_LIMIT",
                         # "BUY_STOP", "SELL_STOP"
    confidence: float    # AI confidence 0-1
    notes: str           # Any additional context from the chart


EXTRACTION_PROMPT = """You are an expert forex/CFD trader analyzing a TradingView chart screenshot.

Extract the following trade setup information from this chart:

1. **Symbol/Pair**: The currency pair, commodity, or index shown (e.g., EURUSD, XAUUSD, US30, GBPJPY). Remove any slash or space — output as a single string like "EURUSD".
2. **Direction**: Is this a BUY (long) or SELL (short) setup?
3. **Entry Price**: The exact entry price level
4. **Stop Loss**: The exact stop loss price level
5. **Take Profit**: The exact take profit price level
6. **Order Type**: Is this a MARKET order (enter now), BUY_LIMIT, SELL_LIMIT, BUY_STOP, or SELL_STOP?
7. **Confidence**: How confident are you in this extraction (0.0 to 1.0)?
8. **Notes**: Any relevant pattern, timeframe, or context visible on the chart

Look for:
- The TradingView Long/Short position tool (green/red boxes with RR ratios)
- Horizontal lines labeled as entry, SL, TP
- Price levels marked on the chart
- The symbol name in the top-left corner of the chart
- Any visible price labels, annotations, or drawing tools
- Arrow indicators, trendlines, or support/resistance zones

RULES:
- If take_profit > entry_price and entry_price > stop_loss → direction is BUY
- If take_profit < entry_price and entry_price < stop_loss → direction is SELL
- If there is a green/red position tool visible, extract values directly from its labels
- If you cannot determine a value, set it to null
- Always return ONLY valid JSON — no markdown fences, no explanation

Return this exact JSON structure:
{
    "symbol": "EURUSD",
    "direction": "BUY",
    "entry_price": 1.08000,
    "stop_loss": 1.07800,
    "take_profit": 1.08600,
    "order_type": "MARKET",
    "confidence": 0.95,
    "notes": "Bullish engulfing on 4H, entry at support retest"
}"""


class VisionExtractor:
    """Uses Gemini Vision AI to extract trade setups from screenshots."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.6-flash",
        thinking_level: str = "low",
        fallback_models: list = None,
        timeout_seconds: float = 20,
    ):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        # Gemini 3 models think at "high" by default, which is the main
        # source of slow analysis. Reading levels off a chart doesn't need
        # deep reasoning, so "low" is used. (Not every model accepts
        # "minimal" — e.g. gemini-3.8-flash rejects it.)
        self.thinking_level = thinking_level
        # Tried in order if a model is overloaded, errors, or is too slow
        self.models = [model] + [
            m for m in (fallback_models or []) if m != model
        ]
        self.timeout = timeout_seconds
        # Slow calls are abandoned (left to finish in the background)
        self._pool = ThreadPoolExecutor(max_workers=4)
        self.last_model_used = ""

    def extract_from_image(self, image_path: str) -> ExtractedTrade:
        """Extract trade setup from a screenshot file on disk."""
        image_path = Path(image_path)

        if not image_path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        image_bytes = image_path.read_bytes()
        mime_type = self._get_mime_type(image_path)
        return self.extract_from_bytes(image_bytes, mime_type)

    def extract_from_bytes(
        self,
        image_bytes: bytes,
        mime_type: str = "image/png",
    ) -> ExtractedTrade:
        """Extract trade setup from raw image bytes (for web uploads).

        Uses the Gemini Interactions API with multimodal input and
        structured JSON output.
        """
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        # Build the JSON schema for structured output
        trade_schema = {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Trading symbol, e.g. EURUSD, XAUUSD",
                },
                "direction": {
                    "type": "string",
                    "enum": ["BUY", "SELL"],
                    "description": "Trade direction",
                },
                "entry_price": {
                    "type": "number",
                    "description": "Entry price level from the chart",
                },
                "stop_loss": {
                    "type": "number",
                    "description": "Stop loss price level",
                },
                "take_profit": {
                    "type": "number",
                    "description": "Take profit price level",
                },
                "order_type": {
                    "type": "string",
                    "enum": [
                        "MARKET",
                        "BUY_LIMIT",
                        "SELL_LIMIT",
                        "BUY_STOP",
                        "SELL_STOP",
                    ],
                    "description": "Order execution type",
                },
                "confidence": {
                    "type": "number",
                    "description": "Extraction confidence 0.0 to 1.0",
                },
                "notes": {
                    "type": "string",
                    "description": "Chart pattern or context notes",
                },
            },
            "required": [
                "symbol",
                "direction",
                "entry_price",
                "stop_loss",
                "take_profit",
                "order_type",
                "confidence",
                "notes",
            ],
        }

        def call(model: str):
            return self._call_model(model, mime_type, image_b64, trade_schema)

        # Try each model in turn; give up on one after `timeout` seconds
        errors = []
        for model in self.models:
            t0 = time.perf_counter()
            future = self._pool.submit(call, model)
            try:
                interaction = future.result(timeout=self.timeout)
            except FutTimeout:
                errors.append(f"{model}: no reply in {self.timeout:.0f}s")
                print(f"⚠️  {errors[-1]} — trying next model")
                continue
            except Exception as e:
                errors.append(f"{model}: {str(e)[:160]}")
                print(f"⚠️  {errors[-1]} — trying next model")
                continue
            self.last_model_used = model
            print(
                f"🧠  {model} answered in {time.perf_counter() - t0:.1f}s"
            )
            # Parse the structured JSON response
            result = json.loads(interaction.output_text)
            return self._parse_result(result)

        raise RuntimeError(
            "All Gemini models failed or were too slow: " + " | ".join(errors)
        )

    def _call_model(self, model, mime_type, image_b64, trade_schema):
        """One Gemini Interactions API call (image + prompt -> JSON)."""
        return self.client.interactions.create(
            model=model,
            input=[
                {
                    "type": "image",
                    "mime_type": mime_type,
                    "data": image_b64,
                },
                {
                    "type": "text",
                    "text": EXTRACTION_PROMPT,
                },
            ],
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": trade_schema,
            },
            generation_config={
                "thinking_level": self.thinking_level,
                "temperature": 0,  # Deterministic number extraction
            },
            store=False,  # No need to persist these interactions
        )

    def _parse_result(self, data: dict) -> ExtractedTrade:
        """Parse JSON response into ExtractedTrade dataclass."""
        symbol = self._normalize_symbol(data.get("symbol", ""))

        return ExtractedTrade(
            symbol=symbol,
            direction=data.get("direction", "").upper(),
            entry_price=float(data.get("entry_price", 0)),
            stop_loss=float(data.get("stop_loss", 0)),
            take_profit=float(data.get("take_profit", 0)),
            order_type=data.get("order_type", "MARKET").upper(),
            confidence=float(data.get("confidence", 0)),
            notes=data.get("notes", ""),
        )

    def _normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol names (e.g., 'EUR/USD' → 'EURUSD')."""
        symbol = symbol.upper().strip().replace("/", "").replace(" ", "")
        # Common display name → MT5 symbol mappings
        mappings = {
            "GOLD": "XAUUSD",
            "SILVER": "XAGUSD",
            "DXY": "DX",
            "NASDAQ": "NAS100",
            "NQ": "NAS100",
            "SP500": "US500",
            "DOW": "US30",
            "DAX": "GER40",
        }
        return mappings.get(symbol, symbol)

    def _get_mime_type(self, path: Path) -> str:
        """Get MIME type from file extension."""
        ext = path.suffix.lower()
        return {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
        }.get(ext, "image/png")
