import sys
from pathlib import Path

from loguru import logger

# Remove default handler
logger.remove()

# Console output
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    level="DEBUG",
)

# File output
_log_path = Path(__file__).resolve().parent.parent / "logs" / "alpha.log"
_log_path.parent.mkdir(parents=True, exist_ok=True)

logger.add(
    str(_log_path),
    rotation="10 MB",
    retention="7 days",
    compression="gz",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{function} - {message}",
    level="DEBUG",
)
