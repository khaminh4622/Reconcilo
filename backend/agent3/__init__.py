"""GhostLedger Agent 3 package."""

__all__ = ["Agent3Service"]


def __getattr__(name: str):
    if name == "Agent3Service":
        from .service import Agent3Service
        return Agent3Service
    raise AttributeError(name)
