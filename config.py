import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Config:
    anthropic_key: str | None  # Claude; tried first
    claude_model: str
    base_url: str | None  # FreeLLMAPI (free tiers); tried when Claude is missing or fails
    api_key: str | None
    model: str  # FreeLLMAPI model name
    mic_threshold: float
    wake_word: str
    data_dir: Path
    models_dir: Path


def load_config(root: Path = ROOT) -> Config:
    load_dotenv(root / ".env")
    data_dir = root / "data"
    models_dir = data_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        anthropic_key=os.getenv("ANTHROPIC_API_KEY") or None,
        claude_model=os.getenv("CLAUDE_MODEL") or "claude-opus-5-5",
        base_url=os.getenv("BASE_URL") or None,
        api_key=os.getenv("API_KEY") or None,
        model=os.getenv("MODEL") or "auto",
        mic_threshold=float(os.getenv("MIC_THRESHOLD") or 0.01),
        wake_word=(os.getenv("WAKE_WORD") or "edith").lower(),
        data_dir=data_dir,
        models_dir=models_dir,
    )


def setup_logging(data_dir: Path) -> None:
    logging.basicConfig(
        filename=data_dir / "edith.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
