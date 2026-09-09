"""Entry point: ``python -m building_modeller.app`` (or the
``building-modeller`` console script). Starts the local Flask server and
opens it in the default browser."""
from __future__ import annotations

import os
import threading
import webbrowser

from .web.server import create_app

HOST = "127.0.0.1"
PORT = 5057


def main() -> None:
    app = create_app()

    if not os.environ.get("BUILDING_MODELLER_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()

    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
