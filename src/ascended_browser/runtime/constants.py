"""Values the browser code reads from the app's constants."""
from .paths import data_dir

DATA_DIR = str(data_dir())
MAX_OUTPUT_CHARS = 10_000
MAX_READ_CHARS = 20_000
MAX_DIFF_LINES = 400
