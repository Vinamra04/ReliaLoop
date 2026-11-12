"""Entry point: `python -m agent`."""

from agent.app.api import create_app
from agent.app.config import AgentSettings
from services.kit.app import serve

if __name__ == "__main__":
    settings = AgentSettings()  # type: ignore[call-arg]  # values come from the environment
    serve(create_app(settings), settings.port)
