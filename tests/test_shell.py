"""Testovi shell skripti (sync_loop.sh, offsite_backup.sh, deploy.sh).

Skripte se pokreću sa stub naredbama na PATH-u (lažni python/sqlite3/timeout/
rclone/rsync/docker/git u tmp direktoriju), bez mreže i bez /data.
"""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SH = shutil.which('sh')
SKRIPTE = ['sync_loop.sh', 'offsite_backup.sh', 'deploy.sh']

pytestmark = pytest.mark.skipif(SH is None, reason='sh nije dostupan')


def _stub(bindir, name, body):
    p = bindir / name
    p.write_text('#!/bin/sh\n' + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


@pytest.fixture
def okolina(tmp_path):
    """tmp /data + bin direktorij sa stubovima; vraća (data, bindir, env)."""
    data = tmp_path / 'data'
    data.mkdir()
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    env = {
        'PATH': f'{bindir}{os.pathsep}{os.environ.get("PATH", "/usr/bin:/bin")}',
        'TZ': 'Europe/Zagreb',
        'HOME': str(tmp_path),
        'SYNC_DATA_DIR': str(data),
        'DB_PATH': str(data / 'hep_energy.db'),
        'CALLS': str(tmp_path / 'calls.log'),
    }
    return data, bindir, env


def _run(script, env, *args, timeout=60):
    return subprocess.run([SH, str(ROOT / script), *args], env=env, cwd=str(ROOT),
                          capture_output=True, text=True, timeout=timeout)


def _calls(env):
    p = Path(env['CALLS'])
    return p.read_text().splitlines() if p.exists() else []


# ── sintaksa ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('script', SKRIPTE)
def test_sh_n(script):
    r = subprocess.run([SH, '-n', str(ROOT / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize('script', ['sync_loop.sh', 'offsite_backup.sh'])
def test_dash_n(script):
    dash = shutil.which('dash')
    if dash is None:
        pytest.skip('dash nije dostupan')
    r = subprocess.run([dash, '-n', str(ROOT / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ── offsite_backup.sh ───────────────────────────────────────────────────────

def _offsite_okolina(okolina, mode):
    data, bindir, env = okolina
    (data / 'backups').mkdir()
    (data / 'backups' / 'hep_energy_20261115_0200.db').write_text('x')
    env.update(OFFSITE_BACKUP_MODE=mode, OFFSITE_BACKUP_DEST='remote:hep/')
    # bez pravog `timeout`-a u testu: stub koji samo preskoči opcije i limit
    _stub(bindir, 'timeout', 'shift; shift; shift\nexec "$@"\n')
    return data, bindir, env


@pytest.mark.parametrize('mode', ['rclone', 'rsync'])
def test_offsite_neuspjeh_ne_pise_marker(okolina, mode):
    data, bindir, env = _offsite_okolina(okolina, mode)
    _stub(bindir, mode, 'echo "$0 $*" >> "$CALLS"\necho "upload exploded" >&2\nexit 7\n')
    r = _run('offsite_backup.sh', env)
    assert r.returncode != 0
    assert not (data / '.last_offsite_ok').exists()
    log = (data / 'sync.log').read_text()
    assert f'ERROR {mode} upload failed' in log
    assert 'upload exploded' in log          # output naredbe i dalje ide u log
    assert f'OK {mode} upload' not in log


@pytest.mark.parametrize('mode', ['rclone', 'rsync'])
def test_offsite_uspjeh_pise_marker(okolina, mode):
    data, bindir, env = _offsite_okolina(okolina, mode)
    _stub(bindir, mode, 'echo "$*" >> "$CALLS"\nexit 0\n')
    r = _run('offsite_backup.sh', env)
    assert r.returncode == 0, r.stdout + r.stderr
    marker = data / '.last_offsite_ok'
    assert marker.exists()
    assert len(marker.read_text().strip()) == 10      # YYYY-MM-DD
    pozivi = _calls(env)
    assert any('hep_energy_20261115_0200.db' in c and 'remote:hep/' in c for c in pozivi)
    # mrežni timeouti su zadani
    if mode == 'rclone':
        assert '--contimeout' in pozivi[0] and '--timeout' in pozivi[0]
    else:
        assert 'ConnectTimeout=' in pozivi[0] and 'ServerAliveInterval=' in pozivi[0]


def test_offsite_neuspjela_retencija_ne_rusi_upload(okolina):
    data, bindir, env = _offsite_okolina(okolina, 'rclone')
    _stub(bindir, 'rclone', 'case "$1" in delete) exit 5 ;; esac\nexit 0\n')
    r = _run('offsite_backup.sh', env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (data / '.last_offsite_ok').exists()


def _sh_broj(script, ime):
    """Default vrijednost numeričke varijable: IME=123 ili IME="${ENV:-123}"."""
    m = re.search(rf'^{ime}=(?:"\$\{{\w+:-)?(\d+)', (ROOT / script).read_text(), re.M)
    assert m, f'{ime} nije nađen u {script}'
    return int(m.group(1))


def test_offsite_limiti_stanu_u_limit_sync_loopa():
    # upload + retencija (svaki + SIGKILL odgoda) < limit koji sync_loop daje
    # cijeloj skripti, a taj < 20 min healthcheck prozora
    kill = _sh_broj('offsite_backup.sh', 'KILL_SEC')
    zbroj = (_sh_broj('offsite_backup.sh', 'MAX_SEC') + kill
             + _sh_broj('offsite_backup.sh', 'RETENTION_SEC') + kill)
    t_offsite = _sh_broj('sync_loop.sh', 'T_OFFSITE')
    assert zbroj < t_offsite, (zbroj, t_offsite)
    assert t_offsite + _sh_broj('sync_loop.sh', 'T_KILL') < 20 * 60


def test_offsite_svaka_naredba_ima_svoj_limit(okolina):
    # stub `timeout` bilježi limit: upload i retencija ne smiju dijeliti isti
    data, bindir, env = _offsite_okolina(okolina, 'rclone')
    _stub(bindir, 'timeout', 'echo "timeout $3 $4 $5" >> "$CALLS"\nshift; shift; shift\nexec "$@"\n')
    _stub(bindir, 'rclone', 'exit 0\n')
    r = _run('offsite_backup.sh', env)
    assert r.returncode == 0, r.stdout + r.stderr
    max_sec = _sh_broj('offsite_backup.sh', 'MAX_SEC')
    ret_sec = _sh_broj('offsite_backup.sh', 'RETENTION_SEC')
    assert _calls(env) == [f'timeout {max_sec} rclone copy', f'timeout {ret_sec} rclone delete']
    assert ret_sec < max_sec


def test_offsite_disabled(okolina):
    data, _bindir, env = okolina
    env['OFFSITE_BACKUP_MODE'] = 'disabled'
    r = _run('offsite_backup.sh', env)
    assert r.returncode == 0
    assert not (data / '.last_offsite_ok').exists()


# ── sync_loop.sh ────────────────────────────────────────────────────────────

PY_STUB = '''echo "python $*" >> "$CALLS"
echo "stub output: $*"
case "$*" in
    *"${STUB_FAIL_MATCH:-__nista__}"*) echo "boom" >&2; exit 3 ;;
esac
exit 0
'''

# `timeout -k K LIMIT cmd...` → samo izvrši cmd; STUB_TIMEOUT_MATCH glumi istek
TIMEOUT_STUB = '''shift; shift; shift
case "$*" in
    *"${STUB_TIMEOUT_MATCH:-__nista__}"*) exit 124 ;;
esac
exec "$@"
'''

SQLITE_STUB = '''echo "sqlite3 $*" >> "$CALLS"
case "$2" in
    .backup*)
        [ "${STUB_SQLITE_FAIL:-0}" = 1 ] && { echo "Error: database is locked" >&2; exit 1; }
        f=${2#.backup \\'}; f=${f%\\'}
        echo data > "$f" ;;
    PRAGMA*) echo ok ;;
esac
exit 0
'''


def _loop_okolina(okolina):
    data, bindir, env = okolina
    _stub(bindir, 'python', PY_STUB)
    _stub(bindir, 'timeout', TIMEOUT_STUB)
    _stub(bindir, 'sqlite3', SQLITE_STUB)
    env.update(SYNC_INTERVAL='0', SYNC_BOOT_DELAY='0', SYNC_MAX_ITER='1',
               SYNC_DAILY_HOUR='0', SYNC_BACKUP_HOUR='0')
    return data, bindir, env


def _py(calls):
    return [c for c in calls if c.startswith('python ')]


def test_sync_loop_jedna_iteracija(okolina):
    data, _bindir, env = _loop_okolina(okolina)
    r = _run('sync_loop.sh', env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (data / '.sync_heartbeat').exists()
    assert (data / '.last_hourly').exists()
    assert (data / '.last_daily_date').exists()

    py = _py(_calls(env))
    # boot: HEP 30 dana → SMA → history → HA
    assert py[:4] == [
        'python /app/hep_scraper.py --dani 30',
        'python /app/sma_scraper.py',
        'python /app/sma_history_import.py',
        'python /app/ha_sender.py',
    ]
    # iteracija: redoslijed koraka i točne komandne linije
    assert py[4:] == [
        'python /app/sma_scraper.py',
        'python /app/ha_push_intel.py',
        'python /app/hep_scraper.py --dani 2',
        'python /app/ha_sender.py',
        'python /app/maintenance.py --backfill-daily --check-freshness --check-anomaly',
        'python /app/sma_history_import.py',
        'python /app/maintenance.py --prune-live --keep-days 45 --bill-reminder '
        '--restore-drill --check-offsite --check-token-age --check-inverter',
    ]
    log = (data / 'sync.log').read_text()
    assert 'stub output: /app/ha_push_intel.py' in log
    assert 'GRESKA' not in log.replace('[offsite] GRESKA', '')   # /app/offsite_backup.sh ne postoji u testu
    # backup ide kroz sqlite3 .backup, nikad cp
    assert any(c.startswith('sqlite3 ') and '.backup' in c for c in _calls(env))
    assert len(list((data / 'backups').glob('hep_energy_*.db'))) == 1
    assert (data / '.last_backup_date').exists()


def test_sync_loop_izbor_sma_izvora(okolina):
    _data, _bindir, env = _loop_okolina(okolina)
    env['SMA_USE_HA_PULL'] = 'true'
    env['SMA_USE_API'] = 'true'
    assert _run('sync_loop.sh', env).returncode == 0
    py = _py(_calls(env))
    assert py[1:4] == [
        'python -m hepapp.ha_puller --only recent',
        'python -m hepapp.ha_puller --only today',
        'python /app/sma_api_scraper.py --only month',
    ]


def test_sync_loop_neuspjeli_korak_se_logira_i_loop_ide_dalje(okolina):
    data, _bindir, env = _loop_okolina(okolina)
    env['STUB_FAIL_MATCH'] = 'ha_push_intel.py'
    r = _run('sync_loop.sh', env)
    assert r.returncode == 0, r.stdout + r.stderr
    log = (data / 'sync.log').read_text()
    assert 'GRESKA exit=3: python /app/ha_push_intel.py' in log
    assert 'boom' in log
    # koraci nakon neuspjelog su se izvršili, heartbeat je dirnut
    py = _py(_calls(env))
    assert py.index('python /app/ha_push_intel.py') < py.index('python /app/hep_scraper.py --dani 2')
    assert (data / '.sync_heartbeat').exists()


def test_sync_loop_timeout_se_logira(okolina):
    data, _bindir, env = _loop_okolina(okolina)
    env['STUB_TIMEOUT_MATCH'] = 'hep_scraper.py --dani 2'
    r = _run('sync_loop.sh', env)
    assert r.returncode == 0, r.stdout + r.stderr
    log = (data / 'sync.log').read_text()
    assert 'TIMEOUT nakon 900s (exit=124): python /app/hep_scraper.py --dani 2' in log
    assert 'python /app/ha_sender.py' in _py(_calls(env))[5:]


def test_sync_loop_restart_ne_ponavlja_satni_i_dnevni_blok(okolina):
    data, _bindir, env = _loop_okolina(okolina)
    assert _run('sync_loop.sh', env).returncode == 0
    Path(env['CALLS']).unlink()
    (data / '.sync_heartbeat').unlink()

    # "restart kontejnera": markeri su na volumenu, isti sat i isti dan.
    # (Ako test padne točno preko granice sata, satni blok se smije ponoviti.)
    hourly_prije = (data / '.last_hourly').read_text()
    assert _run('sync_loop.sh', env).returncode == 0
    py = _py(_calls(env))
    assert not any('--prune-live' in c for c in py)
    assert py.count('python /app/sma_history_import.py') == 1       # samo boot
    if (data / '.last_hourly').read_text() == hourly_prije:
        assert py[4:] == ['python /app/sma_scraper.py', 'python /app/ha_push_intel.py']
    assert not any(c.startswith('sqlite3 ') and '.backup' in c for c in _calls(env))
    assert (data / '.sync_heartbeat').exists()


def test_sync_loop_dnevni_blok_ceka_zadani_sat(okolina):
    data, _bindir, env = _loop_okolina(okolina)
    env['SYNC_DAILY_HOUR'] = '24'      # nikad danas
    env['SYNC_BACKUP_HOUR'] = '24'
    assert _run('sync_loop.sh', env).returncode == 0
    assert not (data / '.last_daily_date').exists()
    assert not any('--prune-live' in c for c in _calls(env))
    assert not (data / 'backups').exists()


def test_sync_loop_neuspjeli_backup_se_logira(okolina):
    data, _bindir, env = _loop_okolina(okolina)
    env['STUB_SQLITE_FAIL'] = '1'
    r = _run('sync_loop.sh', env)
    assert r.returncode == 0, r.stdout + r.stderr
    log = (data / 'sync.log').read_text()
    assert '[backup] GRESKA' in log
    assert 'database is locked' in log
    assert not (data / '.last_backup_date').exists()
    assert list((data / 'backups').glob('*.db')) == []
    assert (data / '.sync_heartbeat').exists()


# ── deploy.sh ───────────────────────────────────────────────────────────────

DOCKER_STUB = '''echo "docker $*" >> "$CALLS"
case "$1 $2" in
    "compose version") exit 0 ;;
    "compose build")   exit "${STUB_BUILD_RC:-0}" ;;
    "inspect "*)       echo "${STUB_HEALTH:-healthy}" ;;
esac
exit 0
'''


def _deploy_okolina(okolina, tmp_path):
    _data, bindir, env = okolina
    _stub(bindir, 'docker', DOCKER_STUB)
    _stub(bindir, 'git', 'echo "git $*" >> "$CALLS"\nexit "${STUB_GIT_RC:-0}"\n')
    env.update(DEPLOY_DIR=str(tmp_path), DEPLOY_HEALTH_TIMEOUT='2', DEPLOY_POLL_SEC='1')
    return env


def _bash():
    bash = shutil.which('bash')
    if bash is None:
        pytest.skip('bash nije dostupan')
    return bash


def _deploy(env):
    return subprocess.run([_bash(), str(ROOT / 'deploy.sh')], env=env,
                          capture_output=True, text=True, timeout=60)


def test_deploy_ok(okolina, tmp_path):
    env = _deploy_okolina(okolina, tmp_path)
    r = _deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'Deploy gotov' in r.stdout
    calls = _calls(env)
    assert calls.index('docker compose build') < calls.index('docker compose up -d')
    assert not any(' down' in c for c in calls)


def test_deploy_neuspjeli_pull_ne_gradi(okolina, tmp_path):
    env = _deploy_okolina(okolina, tmp_path)
    env['STUB_GIT_RC'] = '1'
    r = _deploy(env)
    assert r.returncode != 0
    assert 'Deploy gotov' not in r.stdout
    assert not any('build' in c or ' up' in c for c in _calls(env))


def test_deploy_neuspjeli_build_ne_dira_kontejnere(okolina, tmp_path):
    env = _deploy_okolina(okolina, tmp_path)
    env['STUB_BUILD_RC'] = '1'
    r = _deploy(env)
    assert r.returncode != 0
    assert not any(' up' in c or ' down' in c for c in _calls(env))


def test_deploy_unhealthy_javlja_gresku(okolina, tmp_path):
    env = _deploy_okolina(okolina, tmp_path)
    env['STUB_HEALTH'] = 'unhealthy'
    r = _deploy(env)
    assert r.returncode != 0
    assert 'DEPLOY NEUSPJESAN' in r.stderr
    assert 'Deploy gotov' not in r.stdout
