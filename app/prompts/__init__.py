from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


def load(name: str) -> str:
    """Read app/prompts/<name>.md and return its trimmed contents."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
