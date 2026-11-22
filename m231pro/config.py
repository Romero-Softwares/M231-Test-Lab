"""Caminhos compartilhados da aplicacao."""

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
ASSET_ROOT = PROJECT_ROOT / "assets"
DRIVER_ROOT = PROJECT_ROOT / "driver"
DATA_ROOT = PROJECT_ROOT / "data"
APP_ICON = ASSET_ROOT / "m231pro-icon.ico"
RESULTS_DATABASE = DATA_ROOT / "m231pro-results.sqlite3"
LAST_IMPORTED_PROGRAM = DATA_ROOT / "last-imported-program.json"
