"""Pytest defaults: allow the labeled memory graph double. Product mode does not."""

import os

os.environ.setdefault("EDL_ALLOW_MEMORY_GRAPH", "1")
