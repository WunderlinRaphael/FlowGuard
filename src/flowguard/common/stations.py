"""Station list and upstream mapping, loaded from config/stations.yaml."""

from dataclasses import dataclass
from pathlib import Path

import yaml

# src/flowguard/common/stations.py -> repository root is three levels above this folder.
CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "stations.yaml"
ROLES = {"target", "input", "observe_only"}


@dataclass(frozen=True)
class Station:
    number: str
    name: str
    river: str
    role: str
    parameters: tuple[str, ...]
    up_main: str | None
    up_trib: str | None
    div_q: tuple[str, ...]

    def upstream(self):
        """All station numbers this station uses as upstream inputs."""
        slots = [self.up_main, self.up_trib, *self.div_q]
        return [n for n in slots if n is not None]


def load_stations(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    stations = [
        Station(
            # Station numbers are strings, like in the BAFU API.
            number=str(entry["number"]),
            name=entry["name"],
            river=entry["river"],
            role=entry["role"],
            parameters=tuple(entry["parameters"]),
            up_main=entry.get("up_main"),
            up_trib=entry.get("up_trib"),
            div_q=tuple(entry.get("div_q") or []),
        )
        for entry in raw["stations"]
    ]
    validate(stations)
    return stations


def validate(stations):
    """Raise ValueError if the station list is inconsistent."""
    numbers = [s.number for s in stations]
    if len(numbers) != len(set(numbers)):
        raise ValueError(f"Duplicate station numbers in {numbers}")

    known = by_number(stations)
    for s in stations:
        if s.role not in ROLES:
            raise ValueError(
                f"Station {s.number}: unknown role {s.role!r}, expected one of {ROLES}"
            )
        if s.role == "target" and "W" not in s.parameters:
            raise ValueError(f"Station {s.number}: a target needs the water level W")
        if s.role == "input" and "Q" not in s.parameters:
            raise ValueError(f"Station {s.number}: an input needs the discharge Q")
        for up in s.upstream():
            if up not in known:
                raise ValueError(f"Station {s.number}: upstream station {up} is not in the list")
            if known[up].role == "observe_only":
                raise ValueError(f"Station {s.number}: upstream station {up} is observe_only")


def by_number(stations):
    return {s.number: s for s in stations}


def targets(stations):
    return [s for s in stations if s.role == "target"]
