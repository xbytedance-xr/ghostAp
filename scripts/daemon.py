#!/usr/bin/env python3
import logging
import subprocess
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - DAEMON - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("daemon_monitor.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

def run_with_restart(command: list[str], max_restarts: int = 100):
    restarts = 0
    start_time = time.time()

    while restarts < max_restarts:
        logging.info(f"Starting process: {' '.join(command)}")
        process = subprocess.Popen(command)

        process.wait()
        exit_code = process.returncode

        logging.warning(f"Process exited with code {exit_code}.")

        # If it runs cleanly, exit
        if exit_code == 0:
            logging.info("Clean exit. Daemon stopping.")
            break

        restarts += 1
        logging.info(f"Restarting... (Attempt {restarts}/{max_restarts})")
        time.sleep(5)

    uptime = time.time() - start_time
    logging.info(f"Daemon finished after {uptime:.2f} seconds.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python daemon.py <command_to_run>")
        sys.exit(1)

    cmd = sys.argv[1:]
    run_with_restart(cmd)
