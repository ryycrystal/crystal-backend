import os

# test fixtures seed history-era timestamps, which the orderbook staleness gate
# would read as a mid-reindex indexer. freshness has its own dedicated test
os.environ.setdefault("ORDERBOOK_STALE_SECONDS", "0")
