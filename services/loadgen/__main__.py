"""Entry point: `python -m services.loadgen`."""

from services.kit.app import serve
from services.loadgen.main import build_app
from services.loadgen.settings import LoadgenSettings

if __name__ == "__main__":
    settings = LoadgenSettings()
    serve(build_app(settings), settings.port)
