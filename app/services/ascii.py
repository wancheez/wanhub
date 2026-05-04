import random

import anthropic

from app.prompts import load as load_prompt
from app.schemas.ascii import AsciiArtResponse

MODEL = "claude-haiku-4-5"

THEMES = [
    "a cat",
    "a dog",
    "a friendly robot",
    "a dragon breathing fire",
    "a rocket ship blasting off",
    "a tree in autumn",
    "a snowy mountain",
    "a medieval castle",
    "an ocean wave",
    "a sunflower",
    "a skull",
    "a coffee mug with steam",
    "a laptop computer",
    "an owl on a branch",
    "a fish jumping out of water",
    "a sword stuck in stone",
    "a heart",
    "the sun with rays",
    "the moon and stars",
    "an electric guitar",
    "a tank",
    "a unicorn",
    "a pirate ship",
    "a coiled snake",
    "a butterfly",
    "a bonsai tree",
    "an astronaut helmet",
    "a vintage car",
    "a hot air balloon",
    "a wizard's hat",
    "a dinosaur",
    "a crab",
    "a chess piece",
    "a UFO",
    "a samurai",
]

SYSTEM_PROMPT = load_prompt("ascii")


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def generate_ascii_art() -> AsciiArtResponse:
    subject = random.choice(THEMES)
    client = _get_client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Generate ASCII art of {subject}."}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    return AsciiArtResponse(subject=subject, art=_strip_code_fences(text))
