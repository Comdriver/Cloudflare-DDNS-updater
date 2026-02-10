import os
import time
import subprocess
import sys
from datetime import datetime

def log(msg: str) -> None:
    print(
        f"[runner {datetime.now().isoformat(timespec='seconds')}] {msg}",
        flush=True
    )

def main() -> None:
    delay = int(os.getenv("RUN_EVERY_SECONDS", "300"))  # default 5 minutes
    if delay < 10:
        log(f"RUN_EVERY_SECONDS={delay} too low, forcing minimum 10s.")
        delay = 10

    log(f"Runner started. Interval: {delay} seconds.")

    while True:
        start = time.time()

        result = subprocess.run(
            ["python", "src/main.py"],
            check=False,
        )

        if result.returncode != 0:
            log(f"main.py failed with exit code {result.returncode}. Stopping runner.")
            sys.exit(result.returncode)

        log("main.py finished successfully.")

        elapsed = time.time() - start
        sleep_for = max(0, delay - int(elapsed))

        log(f"Sleeping {sleep_for} seconds...")
        time.sleep(sleep_for)

if __name__ == "__main__":
    main()
