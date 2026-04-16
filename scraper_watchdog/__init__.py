"""scraper-watchdog — automatically detect and repair broken web scrapers."""
from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("scraper-watchdog")
except PackageNotFoundError:
    __version__ = "0.0.0"

from .watcher import ScraperWatcher
from .health_checker import HealthChecker, HealthResult
from .repairer import Repairer
from .sandbox import Sandbox, SandboxResult
from .deployer import Deployer
from .notifier import Notifier

__all__ = [
    "ScraperWatcher",
    "HealthChecker",
    "HealthResult",
    "Repairer",
    "Sandbox",
    "SandboxResult",
    "Deployer",
    "Notifier",
]
