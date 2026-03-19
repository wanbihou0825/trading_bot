"""策略模块"""
from .base import BaseStrategy, StrategyResult, StrategyType
from .endgame import EndgameSweeperStrategy
from .adaptive import AdaptiveStrategyManager

__all__ = [
    "BaseStrategy",
    "StrategyResult",
    "StrategyType",
    "EndgameSweeperStrategy",
    "AdaptiveStrategyManager",
]
