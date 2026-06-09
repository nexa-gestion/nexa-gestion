#!/usr/bin/env python3
# =====================================================================
#  NEXA — Sincronización ISPcube -> Supabase  (robot del cron)
#  v1 (2026-06-09)
#  Lee de ISPcube el estado/deuda de cada cliente conocido y, si cambió,
#  actualiza Nexa (Supabase). Pensado para correr en GitHub Actions.
#
#  CREDENCIALES por variables de entorno (GitHub Secrets) — NUNCA en código:
#    ISPCUBE_BASE     = https://online22.ispcube.com/api
#    ISPCUBE_APIKEY   = (api-key de ISPcube)
#    ISPCUBE_CLIENTID = 651
#    ISPCUBE_USER     = api
#    ISPCUBE_PASS     = (password del usuario api)
#    SUPABASE_URL     = https://xlfntplfhdjoqrofhcwe.supabase.co
#    SUPABASE_KEY     = (anon key de Nexa)
#
#  Uso local de prueba:  exportá las vars y `python3 sync_ispcube.py`
#  (o DRY=1 para no escribir, solo reportar)
# =====================================================================
import os, json, time, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"
BASE   = os.environ.get("ISPCUBE_BASE", "https://online22.ispcube.com/api")
APIKEY = os.environ["ISPCUBE_APIKEY"]
CLIENTID = os.environ.get("ISPCUBE_CLIENTID", "651")
USER   = os.environ.get("ISPCUBE_USER", "api")
PASS   = os.environ["ISPCUBE_PASS"]
SB_URL = os.environ["SUPABASE_URL"]
SB_KEY = os.environ["SUPABASE_KEY"]
DRY    = os.environ.get("DRY", "0") == "1"

STATUS_MAP = {"enabled": "activo", "blocked": "bloqueado", "no_service": "baja"}

def _req(url, data=None, method="GET", headers=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers: h.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    if body is not None: h["Content-Type"] = "application/json"
    return urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h, method=method), timeout=30)

def isp_login():
    r = _req(BASE + "/sanctum/token", {"username": USER, "password": PASS}, "POST",
             {"api-key": APIKEY, "client-id": CLIENTID, "login-type": "api"})
    return json.load(r)["token"]

def isp_headers(token):
    return {"api-key": APIKEY, "client-id": CLIENTID, "login-type": "api",
            "username": USER, "Authorization": "Bearer " + token}

def isp_customer(code, H):
    try:
        r = _req(BASE + "/customer?code=" + urllib.parse.quote(code), headers=H)
        return json.load(r)
    except Exception:
        return None

def sb_get(path):
    r = _req(SB_URL + "/rest/v1/" + path, headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY})
    return json.load(r)

def sb_patch(path, body):
    _req(SB_URL + "/rest/v1/" + path, body, "PATCH",
         {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Prefer": "return=minimal"})

def fetch_all_nexa():
    out, page, size = [], 0, 1000
    while True:
        r = urllib.request.Request(
            SB_URL + "/rest/v1/clientes?select=id,codigo_ispcube,estado,deuda&codigo_ispcube=not.is.null&order=id.asc",
            headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
                     "Range-Unit": "items", "Range": f"{page*size}-{page*size+size-1}", "User-Agent": UA})
        chunk = json.load(urllib.request.urlopen(r, timeout=30))
        out += chunk
        if len(chunk) < size: break
        page += 1
    return out

def main():
    t0 = time.time()
    token = isp_login()
    H = isp_headers(token)
    nexa = fetch_all_nexa()
    print(f"Clientes Nexa a revisar: {len(nexa)}  (DRY={DRY})")

    cambios = []
    def check(c):
        d = isp_customer(c["codigo_ispcube"], H)
        if not d: return ("err", c)
        nuevo_estado = STATUS_MAP.get(d.get("status"))
        nueva_deuda = d.get("duedebt")
        upd = {}
        if nuevo_estado and nuevo_estado != c["estado"]: upd["estado"] = nuevo_estado
        try:
            if nueva_deuda is not None and float(nueva_deuda) != float(c.get("deuda") or 0): upd["deuda"] = float(nueva_deuda)
        except: pass
        return ("upd", c, upd) if upd else ("ok", c)

    errs = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(check, c) for c in nexa]
        for i, f in enumerate(as_completed(futs)):
            res = f.result()
            if res[0] == "err": errs += 1
            elif res[0] == "upd": cambios.append((res[1], res[2]))
            if (i + 1) % 500 == 0: print(f"  revisados {i+1}/{len(nexa)} · cambios {len(cambios)} · err {errs}")

    print(f"\nCambios detectados: {len(cambios)} · errores de lectura: {errs}")
    for c, upd in cambios[:40]:
        print(f"  code {c['codigo_ispcube']}: {upd}")

    if DRY:
        print("\n[DRY] No se escribió nada.")
        return
    for c, upd in cambios:
        try: sb_patch("clientes?id=eq." + str(c["id"]), upd)
        except Exception as e: print(f"  ERROR patch {c['id']}: {e}")
    print(f"\n✓ Sincronización lista. {len(cambios)} clientes actualizados en {round(time.time()-t0)}s.")

if __name__ == "__main__":
    main()
