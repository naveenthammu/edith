from pathlib import Path

MAX_FACTS = 200  # oldest facts drop off first, so the prompt stays small


class Memory:
    """Lasting facts about the user, one per line in a text file the user can read and edit."""

    def __init__(self, path: Path):
        self.path = path

    def facts(self) -> list[str]:
        if not self.path.exists():
            return []
        return [line.strip() for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def add(self, fact: str) -> bool:
        fact = " ".join(fact.split())
        facts = self.facts()
        if not fact or fact.lower() in (f.lower() for f in facts):
            return False
        self._save((facts + [fact])[-MAX_FACTS:])
        return True

    def forget(self, text: str) -> int:
        """Remove every fact containing text (case-insensitive); returns how many went."""
        facts = self.facts()
        keep = [f for f in facts if text.lower() not in f.lower()] if text.strip() else facts
        self._save(keep)
        return len(facts) - len(keep)

    def _save(self, facts: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(f + "\n" for f in facts), encoding="utf-8")
