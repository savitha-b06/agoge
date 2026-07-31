"""Configuration loading. Nothing personal is hardcoded — it all lives in
athlete.yaml, which is gitignored."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    anthropic_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    model_fast: str = field(default_factory=lambda: os.getenv("AGOGE_MODEL_FAST", "claude-haiku-4-5-20251001"))
    model_smart: str = field(default_factory=lambda: os.getenv("AGOGE_MODEL_SMART", "claude-sonnet-5"))
    coros_mcp_url: str = field(default_factory=lambda: os.getenv("COROS_MCP_URL", "https://mcp.coros.com/mcp"))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("AGOGE_DATA_DIR", ROOT / "data")))
    athlete_file: Path = field(default_factory=lambda: Path(os.getenv("AGOGE_ATHLETE_FILE", ROOT / "athlete.yaml")))

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agoge.db"

    @property
    def profile_path(self) -> Path:
        return self.data_dir / "athlete_profile.md"

    @property
    def token_path(self) -> Path:
        return self.data_dir / ".coros_tokens.json"


class Athlete:
    """Thin wrapper over athlete.yaml with the lookups the rest of the code needs."""

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw

    @classmethod
    def load(cls, path: Path) -> "Athlete":
        if not Path(path).exists():
            raise FileNotFoundError(
                f"No athlete config at {path}. Copy athlete.example.yaml to athlete.yaml."
            )
        with open(path) as fh:
            return cls(yaml.safe_load(fh))

    @property
    def name(self) -> str:
        return self.raw.get("name", "Athlete")

    @property
    def zones(self) -> dict[str, list[int]]:
        return self.raw.get("zones", {})

    def zone_bounds(self, zone: str | None = None) -> tuple[int, int]:
        zone = zone or self.raw.get("target_endurance_zone", "z2")
        lo, hi = self.zones[zone]
        return int(lo), int(hi)

    def zone_of(self, bpm: float) -> str:
        for name, (lo, hi) in self.zones.items():
            if lo <= bpm < hi:
                return name
        return "z5" if bpm >= max(h for _, h in self.zones.values()) else "z1"

    @property
    def race_date(self) -> date:
        return _as_date(self.raw["race"]["date"])

    def days_to_race(self, today: date | None = None) -> int:
        return (self.race_date - (today or date.today())).days

    def current_block(self, today: date | None = None) -> dict[str, Any] | None:
        today = today or date.today()
        for block in self.raw.get("blocks", []):
            if _as_date(block["start"]) <= today <= _as_date(block["end"]):
                return block
        return None

    def previous_block(self, today: date | None = None) -> dict[str, Any] | None:
        """Most recent block whose end is strictly before today."""
        today = today or date.today()
        prev = None
        for block in self.raw.get("blocks", []):
            if _as_date(block["end"]) < today:
                if prev is None or _as_date(block["end"]) > _as_date(prev["end"]):
                    prev = block
        return prev

    def next_block(self, today: date | None = None) -> dict[str, Any] | None:
        """Soonest block whose start is strictly after today."""
        today = today or date.today()
        nxt = None
        for block in self.raw.get("blocks", []):
            if _as_date(block["start"]) > today:
                if nxt is None or _as_date(block["start"]) < _as_date(nxt["start"]):
                    nxt = block
        return nxt

    @property
    def injuries(self) -> list[dict[str, Any]]:
        return self.raw.get("injuries", [])

    def injuries_for(self, sport: str) -> list[dict[str, Any]]:
        return [i for i in self.injuries if sport in i.get("prompt_after", [])]

    @property
    def max_ramp_pct(self) -> float:
        return float(self.raw.get("load", {}).get("max_weekly_ramp_pct", 15))

    @property
    def prehab_items(self) -> list[str]:
        return self.raw.get("prehab", {}).get("items", [])

    @property
    def checkpoints(self) -> list[dict[str, Any]]:
        return self.raw.get("checkpoints", [])


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


settings = Settings()
