import os

# Keep the test suite offline: the fallback is on by default in production.
os.environ.setdefault("WIKIDATA_FALLBACK", "false")
