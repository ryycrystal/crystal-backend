# run everytihng

import asyncio

from events import stream_logs

if __name__ == "__main__":
    try:
        asyncio.run(stream_logs())
    except KeyboardInterrupt:
        print("stopped")