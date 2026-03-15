#!/usr/bin/env python3
"""
Orchestrator: HEP scraper + SMA scraper + HA sender
Pokreće se svakih sat
"""
import logging
import sys
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

def run_hep():
    log.info('=== Pokrećem HEP scraper ===')
    try:
        import hep_scraper
        hep_scraper.main()
    except Exception as e:
        log.error(f'HEP scraper greška: {e}', exc_info=True)

def run_sma():
    log.info('=== Pokrećem SMA scraper ===')
    try:
        import sma_scraper
        sma_scraper.main()
    except Exception as e:
        log.error(f'SMA scraper greška: {e}', exc_info=True)

def run_ha():
    log.info('=== Šaljem u Home Assistant ===')
    try:
        import ha_sender
        ha_sender.main()
    except Exception as e:
        log.error(f'HA sender greška: {e}', exc_info=True)

if __name__ == '__main__':
    run_hep()
    run_sma()
    run_ha()
    log.info('=== Sync završen ===')
