"""/setup — prvi-put konfiguracijski wizard.

Aktivira se ako u config tablici nije postavljen `_setup_complete=1`.
Postojeće instalacije se automatski označavaju kao gotove pri prvom bootu
(vidi db.py:init_db) tako da se wizard ne pojavljuje retroaktivno.
"""

import os

from flask import Blueprint, jsonify, redirect, request

from ..core import admin_required
from ..db import set_config

bp = Blueprint('wizard', __name__)

SETUP_PAGE = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>InfoBot Energija — Setup</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600;700&family=IBM+Plex+Mono&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080c12;color:#d0dde8;font-family:'IBM Plex Sans',sans-serif;
     min-height:100vh;padding:30px 20px;display:flex;justify-content:center}
.wrap{width:100%;max-width:640px}
.box{background:#0f1623;border:1px solid #1f2d3d;border-radius:14px;padding:32px 28px;box-shadow:0 20px 60px rgba(0,0,0,0.5)}
.logo{display:flex;align-items:center;justify-content:center;margin-bottom:20px}
.logo-text{font-size:28px;font-weight:900;letter-spacing:-1px}
.logo-info{color:#00d4ff}.logo-bot{color:#e63329}
h1{font-size:20px;font-weight:700;margin-bottom:8px;text-align:center}
.subtitle{font-size:12px;color:#526070;text-align:center;margin-bottom:24px;text-transform:uppercase;letter-spacing:1px}
section{border-top:1px solid #1f2d3d;padding-top:20px;margin-top:20px}
section:first-of-type{border-top:none;margin-top:0;padding-top:0}
h2{font-size:14px;font-weight:600;color:#22d3ee;text-transform:uppercase;letter-spacing:.6px;margin-bottom:12px}
.hint{font-size:12px;color:#526070;margin-bottom:14px}
label{display:block;font-size:11px;color:#526070;text-transform:uppercase;letter-spacing:.6px;margin-bottom:5px}
input{width:100%;padding:10px 13px;background:#080c12;border:1px solid #1f2d3d;
      border-radius:7px;color:#d0dde8;font-size:13px;margin-bottom:12px;
      font-family:'IBM Plex Mono',monospace}
input:focus{outline:none;border-color:#22d3ee}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
button{padding:12px 22px;border:none;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;letter-spacing:.3px}
.btn-primary{background:linear-gradient(135deg,#22d3ee,#0891b2);color:#000}
.btn-skip{background:transparent;color:#526070;border:1px solid #1f2d3d}
.actions{display:flex;justify-content:space-between;align-items:center;margin-top:24px;gap:10px}
.msg{padding:10px 14px;border-radius:7px;font-size:13px;margin-top:14px;display:none}
.msg.err{background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.3);color:#f87171}
.msg.ok{background:rgba(34,211,238,.1);border:1px solid rgba(34,211,238,.3);color:#22d3ee}
</style></head><body>
<div class="wrap"><div class="box">
  <div class="logo"><div class="logo-text"><span class="logo-info">INFO</span><span class="logo-bot">BOT</span></div></div>
  <h1>Početni setup</h1>
  <div class="subtitle">Konfiguracija prije prvog korištenja</div>

  <section>
    <h2>🏭 HEP ODS</h2>
    <div class="hint">Obavezno za HEP scraper. Šifra je sa zadnje uplatnice.</div>
    <div class="row">
      <div><label>Email</label><input id="hep_user" placeholder="vas@email.hr" autocomplete="username"></div>
      <div><label>Lozinka</label><input id="hep_pass" type="password" autocomplete="new-password"></div>
    </div>
    <label>Šifra mjernog mjesta</label><input id="hep_sifra" placeholder="10-znamenkasta šifra">
  </section>

  <section>
    <h2>☀️ SMA Sunny Portal <span style="color:#526070;font-weight:400;text-transform:none">(opcionalno)</span></h2>
    <div class="row">
      <div><label>Email</label><input id="sma_user" placeholder="vas@email.hr"></div>
      <div><label>Lozinka</label><input id="sma_pass" type="password"></div>
    </div>
    <div class="row">
      <div><label>Plant ID</label><input id="sma_plant"></div>
      <div><label>Inverter 1 ID</label><input id="sma_inv1"></div>
    </div>
    <label>Inverter 2 ID</label><input id="sma_inv2">
  </section>

  <section>
    <h2>🏠 Home Assistant <span style="color:#526070;font-weight:400;text-transform:none">(opcionalno)</span></h2>
    <label>URL</label><input id="ha_url" placeholder="https://homeassistant.local:8123">
    <label>Long-lived access token</label><input id="ha_token" type="password" placeholder="eyJ…">
  </section>

  <section>
    <h2>⚡ Tarifa</h2>
    <label>VT/NT omjer (% potrošnje u skupljoj tarifi)</label>
    <input id="vt_udio" type="number" min="0" max="100" step="1" value="45" placeholder="45">
    <div class="hint">Default 45%. Tipično: ljeto 35%, prosjek 45%, zima 55%.</div>
  </section>

  <div class="msg" id="msg"></div>

  <div class="actions">
    <button class="btn-skip" onclick="finish(true)">Preskoči — riješit ću kasnije</button>
    <button class="btn-primary" onclick="finish(false)">Spremi i nastavi →</button>
  </div>
</div></div>
<script>
async function finish(skip){
  const msg = document.getElementById('msg');
  msg.style.display = 'none';
  const data = skip ? {skip:true} : {
    HEP_USERNAME: hep_user.value, HEP_PASSWORD: hep_pass.value, HEP_SIFRA: hep_sifra.value,
    SMA_USERNAME: sma_user.value, SMA_PASSWORD: sma_pass.value,
    SMA_PLANT_ID: sma_plant.value, SMA_INV1_ID: sma_inv1.value, SMA_INV2_ID: sma_inv2.value,
    HA_URL: ha_url.value, HA_TOKEN: ha_token.value,
    VT_UDIO_PERC: vt_udio.value,
  };
  try {
    const r = await fetch('/api/setup/complete', {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json','Origin':location.origin},
      body: JSON.stringify(data),
    });
    const j = await r.json();
    if (j.ok) {
      msg.className='msg ok';msg.textContent='Spremljeno. Idem na dashboard…';msg.style.display='block';
      setTimeout(()=>location.href='/', 700);
    } else {
      msg.className='msg err';msg.textContent='Greška: '+(j.error||'nepoznato');msg.style.display='block';
    }
  } catch(e){
    msg.className='msg err';msg.textContent='Network error: '+e;msg.style.display='block';
  }
}
</script>
</body></html>'''


@bp.route('/setup')
@admin_required
def setup_page():
    return SETUP_PAGE


@bp.route('/api/setup/complete', methods=['POST'])
@admin_required
def api_setup_complete():
    """Spremi konfiguraciju i označi setup gotovim."""
    data = request.get_json() or {}

    if not data.get('skip'):
        # Zapiši .env (samo neprazna polja)
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env')
        existing = {}
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        existing[k.strip()] = v.strip()

        env_keys = ['HEP_USERNAME', 'HEP_PASSWORD', 'HEP_SIFRA',
                    'SMA_USERNAME', 'SMA_PASSWORD', 'SMA_PLANT_ID',
                    'SMA_INV1_ID', 'SMA_INV2_ID', 'HA_URL', 'HA_TOKEN']
        for k in env_keys:
            v = (data.get(k) or '').strip()
            if v:
                existing[k] = v
                os.environ[k] = v

        lines = ['# HEP Energy Monitor — generated by setup wizard\n']
        for k in env_keys + ['DASHBOARD_PASSWORD', 'SECRET_KEY', 'DB_PATH']:
            lines.append(f'{k}={existing.get(k, "")}\n')
        try:
            with open(env_path, 'w') as f:
                f.writelines(lines)
            os.chmod(env_path, 0o600)
        except Exception as e:
            return jsonify({'ok': False, 'error': f'.env write failed: {e}'}), 500

        if data.get('VT_UDIO_PERC'):
            try:
                set_config('VT_UDIO_PERC', str(int(data['VT_UDIO_PERC'])))
            except Exception:
                pass

    set_config('_setup_complete', '1')
    return jsonify({'ok': True})
