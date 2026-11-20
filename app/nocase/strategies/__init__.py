"""Interchangeable strategies per stage (episodes, case_link, assign); see base.py."""
from app.nocase.strategies import assign, case_link, episodes  # noqa: F401  (registers the strategies)
from app.nocase.strategies.base import REGISTRY, Context  # noqa: F401
