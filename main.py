import sys, asyncio
from stream import stream_logs

if __name__ == "__main__":
    start_block = int(sys.argv[1], 0) if len(sys.argv) > 1 else None
    try:
        asyncio.run(stream_logs(start_block))
    except KeyboardInterrupt:
        print("stopped")
