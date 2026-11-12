"""Entry point: `python -m services.orders`."""

from services.kit.app import serve
from services.orders.main import build_app
from services.orders.settings import OrdersSettings

if __name__ == "__main__":
    settings = OrdersSettings()
    serve(build_app(settings), settings.port)
