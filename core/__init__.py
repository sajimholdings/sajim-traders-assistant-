"""Trade Assistant Core Package — configuration loader."""
import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config() -> dict:
    """Load configuration from YAML file."""
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def save_config(config: dict):
    """Save updated configuration back to YAML file."""
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
