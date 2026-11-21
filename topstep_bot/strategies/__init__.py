from .base import Strategy, StrategyContext
from .orb import OpeningRangeBreakout
from .vwap_pullback import VWAPPullback

REGISTRY = {
    OpeningRangeBreakout.name: OpeningRangeBreakout,
    VWAPPullback.name: VWAPPullback,
}


def build_strategies(cfg: dict, instrument, clock) -> list[Strategy]:
    scfg = cfg.get("strategies", {})
    out: list[Strategy] = []
    for name in scfg.get("enabled", []):
        if name not in REGISTRY:
            raise ValueError(f"unknown strategy '{name}'. Available: {sorted(REGISTRY)}")
        out.append(REGISTRY[name](scfg.get(name, {}), instrument, clock))
    if not out:
        raise ValueError("no strategies enabled (strategies.enabled is empty)")
    return out


__all__ = [
    "Strategy",
    "StrategyContext",
    "OpeningRangeBreakout",
    "VWAPPullback",
    "REGISTRY",
    "build_strategies",
]
