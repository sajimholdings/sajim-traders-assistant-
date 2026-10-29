# Sajim Traders Assistant

AI trade assistant for the Sajim Traders desk: drop a chart screenshot, let Gemini Vision read the setup, size it with the risk engine, place it on MetaTrader 5 and journal it to Notion.

## Run

```bash
python -m venv venv && venv\Scripts\activate      # Windows
pip install -r requirements.txt
copy .env.example .env                              # then fill in the keys
python app.py
```

Open http://127.0.0.1:5000.

## Configuration

- `.env` — `GEMINI_API_KEY`, `NOTION_API_KEY`, `NOTION_DATABASE_ID` (never committed).
- `config.yaml` — risk limits (risk %, target R:R, max lot), spread handling, MT5 terminal, Notion and vision model settings.

## Layout

| Path | What |
|---|---|
| `app.py` | Flask entry point |
| `core/` | MT5 connector, risk engine, trade executor, trade log, Notion journal, vision extractor |
| `web/` | Routes, templates and static assets for the drag-and-drop UI |
| `tests/` | Risk engine tests (`pytest`) |

## Tests

```bash
pytest
```
