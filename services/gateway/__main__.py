"""Entry point: `python -m services.gateway`."""

from services.gateway.main import build_app
from services.gateway.settings import GatewaySettings
from services.kit.app import serve

if __name__ == "__main__":
    settings = GatewaySettings()
    serve(build_app(settings), settings.port)
