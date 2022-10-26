import logging
import os
import time

from anarchyrunner import AnarchyRunner

logging.basicConfig(
    format = '%(asctime)s %(levelname)s %(message)s',
    level = os.environ.get('LOG_LEVEL', 'INFO')
)

def main():
    anarchy_runner = AnarchyRunner()
    anarchy_runner.setup_runner()
    while True:
        try:
            anarchy_runner.run_loop()
        except Exception:
            logging.exception('Unhandled exception in run loop!')
            time.sleep(60)

if __name__ == '__main__':
    main()
