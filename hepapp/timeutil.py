"""Vremenske zone i tarifna doba — zajednički helperi za blueprinte.

Konvencije u bazi (vidi i hep_scraper / sma_* pisače):
  - HEP tablice (ocitanja_15min/satna.ts, ocitanja_dnevna.datum): LOKALNO vrijeme,
    naivno, 'YYYY-MM-DDTHH:MM:SS' / 'YYYY-MM-DD'.
  - sma_live.ts: UTC s offsetom ('...+00:00'); sma_15min.ts: UTC sa 'Z'.
  - sma_dnevna.datum: lokalni kalendarski dan.
  - sync_log.ts, config.updated: SQLite datetime('now') = UTC s razmakom.

SQLite datetime('now') vraća UTC s RAZMAKOM ('YYYY-MM-DD HH:MM:SS'), pa je
usporedba s 'T' formatom leksički kriva ('T' > ' ' -> cijeli današnji dan
ispadne "u budućnosti"). Zato se ovdje "sada" uvijek gradi u istom formatu
kao kolona s kojom se uspoređuje.

SQL izrazi s 'localtime'/'utc' ovise o TZ procesa — oba kontejnera rade s
TZ=Europe/Zagreb.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Zagreb')

_FMT_T = '%Y-%m-%dT%H:%M:%S'


def now_local() -> datetime:
    """Trenutno hrvatsko vrijeme (aware)."""
    return datetime.now(TZ)


def today_local() -> str:
    """Današnji lokalni datum 'YYYY-MM-DD'."""
    return now_local().strftime('%Y-%m-%d')


def _mods(modifiers) -> str:
    return ''.join(f",'{m}'" for m in modifiers)


def sql_now_local(*modifiers) -> str:
    """SQL izraz za "sada" u HEP formatu (lokalno, 'T').

    Dodatni modifikatori idu NAKON 'localtime', npr. sql_now_local('-7 days').
    """
    return f"strftime('{_FMT_T}','now','localtime'{_mods(modifiers)})"


def sql_now_utc(*modifiers) -> str:
    """SQL izraz za "sada" u UTC 'T' formatu — za usporedbu sa sma_live/sma_15min.

    Nema sufiksa zone: 'YYYY-MM-DDTHH:MM:SS' je prefiks i '+00:00' i 'Z' oblika,
    pa leksička usporedba radi za oba.
    """
    return f"strftime('{_FMT_T}','now'{_mods(modifiers)})"


def sql_today_local(*modifiers) -> str:
    """SQL izraz za lokalni današnji datum, npr. sql_today_local('-90 days')."""
    return f"date('now','localtime'{_mods(modifiers)})"


SQL_NOW_LOCAL = sql_now_local()
SQL_NOW_UTC = sql_now_utc()
SQL_TODAY_LOCAL = sql_today_local()


def vt_sql(col: str = 'ts', vt_od: int = 7, vt_do: int = 21,
           kraj_intervala: bool = False) -> str:
    """SQL boolean izraz: "ovaj red (lokalni ts) pada u višu tarifu (VT)".

    VT je 07-21 po zimskom i 08-22 po ljetnom računanju, tj. cijele godine
    06:00-20:00 UTC. vt_od/vt_do su ZIMSKI (CET) sati iz configa
    (TARIFA_VT_OD/TARIFA_VT_DO), pa je UTC raspon sati [vt_od-1, vt_do-2].
    Modifikator 'utc' tumači ts kao lokalno vrijeme procesa i sam rješava DST.

    kraj_intervala=False: red je označen POČETKOM intervala (ocitanja_satna).
    kraj_intervala=True:  red je označen KRAJEM intervala (ocitanja_15min:
                          uzorak T pokriva (T-15min, T]) -> pomak -15 min.
    """
    lo, hi = int(vt_od) - 1, int(vt_do) - 2
    pomak = ", '-15 minutes'" if kraj_intervala else ''
    return (f"(CAST(strftime('%H', {col}, 'utc'{pomak}) AS INTEGER) "
            f"BETWEEN {lo} AND {hi})")


def raspon_mjeseca(mjesec: str):
    """'YYYY-MM' -> ('YYYY-MM-01', 'YYYY-MM-01' idućeg mjeseca).

    Za sargabilne predikate (ts >= ? AND ts < ?) umjesto substr(ts,1,7) = ?.
    Baca ValueError za neispravan format.
    """
    if len(mjesec) != 7 or mjesec[4] != '-':
        raise ValueError(f'neispravan mjesec: {mjesec!r}')
    god, mj = int(mjesec[:4]), int(mjesec[5:])
    if not (1 <= god <= 9998 and 1 <= mj <= 12):
        raise ValueError(f'neispravan mjesec: {mjesec!r}')
    iduci = (god + 1, 1) if mj == 12 else (god, mj + 1)
    return f'{god:04d}-{mj:02d}-01', f'{iduci[0]:04d}-{iduci[1]:02d}-01'


def utc_raspon_dana(od: str, do: str = None):
    """UTC granice lokalnih dana [od, do] kao ('...T..', '...T..'), kraj isključiv.

    Za tablice s UTC ts (sma_live, sma_15min): lokalni dan 2026-11-16 je
    ['2026-11-15T23:00:00', '2026-11-16T23:00:00'). Granice su bez sufiksa
    zone (prefiks i 'Z' i '+00:00' oblika) pa se uspoređuju leksički.
    Baca ValueError za neispravan datum, uključujući rubove raspona
    datetime-a ('0001-01-01', '9999-12-31') gdje pomak dana/zone prelijeva.
    """
    d0 = datetime.strptime(od, '%Y-%m-%d').replace(tzinfo=TZ)
    try:
        d1 = (datetime.strptime(do or od, '%Y-%m-%d') + timedelta(days=1)).replace(tzinfo=TZ)
        return (d0.astimezone(timezone.utc).strftime(_FMT_T),
                d1.astimezone(timezone.utc).strftime(_FMT_T))
    except OverflowError as e:
        raise ValueError(f'datum izvan raspona: {od!r}..{do or od!r}') from e
