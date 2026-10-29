"""AI Trade Assistant — Main Entry Point.

Starts the Flask web server that serves the drag-and-drop UI
and connects to MT5, Gemini Vision AI, and Notion.
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask

# Load environment variables from .env file
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder="web/templates",
        static_folder="web/static",
    )

    # Register API routes
    from web.routes import api
    app.register_blueprint(api)

    return app


if __name__ == "__main__":
    app = create_app()

    print()
    print("=" * 56)
    print("  🤖  AI Trade Assistant")
    print("=" * 56)
    print()
    print("  📍  Open:  http://localhost:5000")
    print()

    # Check for required env vars
    if not os.getenv("GEMINI_API_KEY"):
        print("  ⚠️   GEMINI_API_KEY not set in .env")
        print("       Get one at: https://aistudio.google.com/apikey")
    else:
        print("  ✅  Gemini API key loaded")

    if not os.getenv("NOTION_API_KEY"):
        print("  ⚠️   NOTION_API_KEY not set — journaling disabled")
    else:
        print("  ✅  Notion API key loaded")

    print()
    print("  💡  Make sure MetaTrader 5 is running and logged in")
    print("=" * 56)
    print()

    app.run(host="0.0.0.0", port=5000, debug=True)
