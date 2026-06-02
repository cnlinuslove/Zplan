__all__ = ["run_pick_agent"]


def __getattr__(name: str):
    if name == "run_pick_agent":
        from pick_agent.main import run_pick_agent

        return run_pick_agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
