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
        # hep_scraper nema main() — ulazna točka je sync() (True = uspjeh)
        ok = hep_scraper.sync()
        if not ok:
            log.error('HEP scraper: sync neuspješan (vidi sync_log / hep_scraper.log)')
        return bool(ok)
    except Exception as e:
        log.error(f'HEP scraper greška: {e}', exc_info=True)
        return False

def run_sma():
    log.info('=== Pokrećem SMA scraper ===')
    try:
        import sma_scraper
        sma_scraper.main()
        return True
    except Exception as e:
        log.error(f'SMA scraper greška: {e}', exc_info=True)
        return False

def run_ha():
    log.info('=== Šaljem u Home Assistant ===')
    try:
        import ha_sender
        ok = ha_sender.main()
        if not ok:
            log.error('HA sender: slanje neuspješno')
        return bool(ok)
    except Exception as e:
        log.error(f'HA sender greška: {e}', exc_info=True)
        return False

if __name__ == '__main__':
    # Svaki korak se izvršava neovisno; exit kod != 0 ako je ijedan pao
    rezultati = [run_hep(), run_sma(), run_ha()]
    log.info('=== Sync završen ===')
    sys.exit(0 if all(rezultati) else 1)
