import logging
import os
from datetime import datetime

# Setup log directory
log_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "logs"))
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"app_{datetime.now().strftime('%Y-%m-%d')}.log")

logger = logging.getLogger("job_assistant")
logger.setLevel(logging.DEBUG)

# Prevent adding handlers multiple times if imported multiple times
if not logger.handlers:
    # File handler (persists the full run history, not just errors)
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(logging.INFO)
    fh_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(module)s - %(message)s')
    fh.setFormatter(fh_formatter)

    # Console handler (logs INFO and above)
    import sys
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch_formatter = logging.Formatter('%(message)s') # Keep console clean
    ch.setFormatter(ch_formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)

def get_logger():
    return logger
