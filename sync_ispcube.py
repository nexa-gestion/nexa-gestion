#!/usr/bin/env python3
# =====================================================================
#  NEXA — Sincronización ISPcube -> Supabase  (robot del cron)
#  v2 (2026-06-09) — BAJO CONSUMO DE API
#  Usa el endpoint CACHEADO de listado: /api/customers/customers_list
#  (limit+offset). Un sync completo = ~ceil(clientes/500) llamadas (~9),
#  NO una por cliente. ISPcube lo recomienda y cobra por request
#  (incluidas = 2,5 x conexiones activas por mes, corte día 28).
#
#  Hace: actualizar estado+deuda de los existentes (por codigo_ispcube)
#        e INSERTAR los clientes nuevos que aparezcan en ISPcube.
#
#  Credenciales por variables de entorno (GitHub Secrets):
#    ISPCUBE_BASE, ISPCUBE_APIKEY, ISPCUBE_CLIENTID, ISPCUBE_USER,
#    ISPCUBE_PASS, SUPABASE_URL, SUPABASE_KEY     (DRY=1 = no escribe)
# =====================================================================
import os, json, urllib.request, urllib.parse, time

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"
BASE   = os.environ.get("ISPCUBE_BASE", "https://online22.ispcube.com/api")
APIKEY = os.environ["ISPCUBE_APIKEY"]
CLIENTID = os.environ.get("ISPCUBE_CLIENTID", "651")
USER   = os.environ.get("ISPCUBE_USER", "api")
PASS   = os.environ["ISPCUBE_PASS"]
SB_URL = os.environ.get("SUPABASE_URL", "https://xlfntplfhdjoqrofhcwe.supabase.co")
SB_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhsZm50cGxmaGRqb3Fyb2ZoY3dlIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA5NTIyMjEsImV4cCI6MjA5NjUyODIyMX0.7uqzHQyrir23uCQ_GUabvX5kavr3VC_6UzaOizdZJI4")
DRY    = os.environ.get("DRY", "0") == "1"
PAGE   = int(os.environ.get("PAGE_LIMIT", "500"))

STATUS_MAP = {"enabled": "activo", "blocked": "bloqueado", "no_service": "baja"}

# Campos de conexión FTTH (nombres candidatos; el modo DIAG confirma los reales)
CAJA_KEYS   = ["ftthbox_name", "ftthbox", "caja_fibra", "fiber_box", "nap", "caja"]
PUERTO_KEYS = ["fiber_port", "port", "puerto", "puerto_fibra", "ftth_port", "fiberport"]
PREC_KEYS   = ["precinto", "seal", "seal_number", "precinto_number", "seal_code"]

def _first(c, keys):
    for k in keys:
        v = c.get(k)
        if v not in (None, "", 0, "0"):
            return str(v).strip()
    return None

def conex(c, portmap=None):
    cns = c.get("connections") or []
    cn0 = cns[0] if (cns and isinstance(cns[0], dict)) else {}
    caja = _first(c, CAJA_KEYS) or cn0.get("ftthbox_name")
    prec = cn0.get("seal") or _first(c, PREC_KEYS)
    pid = cn0.get("ftth_port_id")
    nro = (portmap or {}).get(pid) if pid is not None else None
    return {"caja_nap": (str(caja).strip() if caja else None),
            "precinto": (str(prec).strip() if prec else None),
            "puerto": (str(nro).strip() if nro not in (None, "") else None)}

def _req(url, data=None, method="GET", headers=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers: h.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    if body is not None: h["Content-Type"] = "application/json"
    return urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h, method=method), timeout=60)

# ---- ISPcube ----
def isp_login():
    r = _req(BASE + "/sanctum/token", {"username": USER, "password": PASS}, "POST",
             {"api-key": APIKEY, "client-id": CLIENTID, "login-type": "api"})
    return json.load(r)["token"]

def isp_list_all(token):
    H = {"api-key": APIKEY, "client-id": CLIENTID, "login-type": "api",
         "username": USER, "Authorization": "Bearer " + token}
    out, offset, calls = [], 0, 0
    while True:
        r = _req(BASE + f"/customers/customers_list?limit={PAGE}&offset={offset}", headers=H)
        d = json.load(r); calls += 1
        chunk = d if isinstance(d, list) else (d.get("data") or d.get("customers") or [])
        out += chunk
        if len(chunk) < PAGE: break
        offset += PAGE
    print(f"ISPcube: {len(out)} clientes en {calls} llamadas API")
    return out

def isp_ftthboxes(token):
    # Trae TODAS las cajas FTTH con sus puertos en 1 llamada -> mapa {ftth_port_id: nro}
    H = {"api-key": APIKEY, "client-id": CLIENTID, "login-type": "api",
         "username": USER, "Authorization": "Bearer " + token}
    try:
        d = json.load(_req(BASE + "/ftthboxes/ftthboxes_list", headers=H))
    except Exception as e:
        print("WARN ftthboxes_list:", e); return {}
    boxes = d if isinstance(d, list) else (d.get("data") or [])
    pm = {}
    for b in boxes:
        for p in (b.get("ftth_ports") or []):
            if p.get("id") is not None: pm[p["id"]] = p.get("nro")
    print(f"ISPcube: {len(boxes)} cajas FTTH, {len(pm)} puertos (1 llamada)")
    return pm

# ---- Supabase ----
def sb_headers(extra=None):
    h = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "User-Agent": UA}
    if extra: h.update(extra)
    return h

def sb_get(path):
    return json.load(_req(SB_URL + "/rest/v1/" + path, headers=sb_headers()))

def sb_patch(path, body):
    _req(SB_URL + "/rest/v1/" + path, body, "PATCH",
         {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Prefer": "return=minimal"})

def sb_post(path, rows):
    _req(SB_URL + "/rest/v1/" + path, rows, "POST",
         {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Prefer": "return=minimal"})

def fetch_nexa():
    out, page = {}, 0
    while True:
        r = urllib.request.Request(
            SB_URL + "/rest/v1/clientes?select=id,codigo_ispcube,estado,deuda,caja_nap,puerto,precinto&codigo_ispcube=not.is.null&order=id.asc",
            headers=sb_headers({"Range": f"{page*1000}-{page*1000+999}"}))
        chunk = json.load(urllib.request.urlopen(r, timeout=60))
        for c in chunk: out[c["codigo_ispcube"]] = c
        if len(chunk) < 1000: break
        page += 1
    return out

def plan_map():
    return {p["nombre"]: p["id"] for p in sb_get("planes?select=id,nombre")}

def barrio_emp_map():
    return {b["nombre"]: b.get("empresa_sugerida") for b in sb_get("barrios?select=nombre,empresa_sugerida")}

def barrio_de(addr):
    addr = (addr or "").strip()
    return addr.split(" - ")[0].strip() if " - " in addr else None

def nuevo_cliente(c, planes, bemap, portmap=None):
    code = str(c.get("code") or "").strip()
    addr = c.get("address") or c.get("tax_residence")
    barrio = barrio_de(addr)
    plan = c.get("plan_name")
    return {
        "codigo_ispcube": code or None,
        # numero_cliente NO se setea: lo asigna la secuencia de Nexa (cliente_numero_seq, 900000+).
        # El número de ISPcube vive aparte en codigo_ispcube. Así nunca chocan los dos espacios.
        "nombre": c.get("name"),
        "doc_numero": c.get("doc_number"),
        "telefono": (c.get("phones") or [{}])[0].get("number") if c.get("phones") else None,
        "email": (c.get("contact_emails") or [{}])[0].get("email") if c.get("contact_emails") else None,
        "domicilio_full": addr, "barrio": barrio,
        "empresa": bemap.get(barrio),
        "nodo": c.get("node_name"),
        "plan_id": planes.get(plan),
        "tecnologia": "FTTH" if "FTTH" in (plan or "").upper() else ("INALAMBRICO" if "WIFI" in (plan or "").upper() else None),
        "estado": STATUS_MAP.get(c.get("status"), "prospecto"),
        "deuda": _f(c.get("duedebt")),
        "lat": _f(c.get("lat")), "lng": _f(c.get("lng")),
        "fecha_alta": (c.get("start_date") or "")[:10] or None,
        **conex(c, portmap),
    }

def _f(v):
    try: return float(v)
    except: return None

def main():
    t0 = time.time()
    # MODO DIAGNÓSTICO: DIAG=001906 -> imprime todos los campos de ese cliente
    # (para descubrir cómo se llaman caja/puerto/precinto en customers_list)
    if os.environ.get("DIAG"):
        token = isp_login(); isp = isp_list_all(token)
        code = os.environ["DIAG"].strip().lstrip("0")
        cli = next((x for x in isp if str(x.get("code") or "").strip().lstrip("0") == code), (isp[0] if isp else None))
        if not cli: print("No encontré ese cliente."); return
        print("=== CAMPOS del cliente", cli.get("code"), "===")
        for k in sorted(cli.keys()): print(f"  {k} = {cli[k]!r}")
        print("\n=== conex() detecta:", conex(cli))
        return
    token = isp_login()
    isp = isp_list_all(token)
    portmap = isp_ftthboxes(token)
    nexa = fetch_nexa()
    planes = plan_map(); bemap = barrio_emp_map()
    print(f"Nexa: {len(nexa)} clientes con codigo_ispcube")

    updates, nuevos = [], []
    for c in isp:
        code = str(c.get("code") or "").strip()
        if not code: continue
        est = STATUS_MAP.get(c.get("status"))
        deu = _f(c.get("duedebt"))
        if code in nexa:
            cur = nexa[code]; upd = {}
            if est and est != cur["estado"]: upd["estado"] = est
            if deu is not None and float(deu) != float(cur.get("deuda") or 0): upd["deuda"] = deu
            cx = conex(c, portmap)
            for f in ("caja_nap", "puerto", "precinto"):
                if cx[f] and cx[f] != (cur.get(f) or None): upd[f] = cx[f]
            if upd: updates.append((cur["id"], upd))
        else:
            nuevos.append(nuevo_cliente(c, planes, bemap, portmap))

    print(f"\nA actualizar: {len(updates)} · clientes NUEVOS a insertar: {len(nuevos)}")
    for n in nuevos[:20]:
        print(f"  NUEVO {n['codigo_ispcube']} {n['nombre']} ({n['estado']})")

    if DRY:
        print("\n[DRY] No se escribió nada.")
        return
    for cid, upd in updates:
        try: sb_patch("clientes?id=eq." + str(cid), upd)
        except Exception as e: print(f"  ERROR upd {cid}: {e}")
    # nuevos en lotes de 200 (claves homogéneas)
    for i in range(0, len(nuevos), 200):
        try: sb_post("clientes", nuevos[i:i+200])
        except Exception as e: print(f"  ERROR insert lote {i}: {e}")
    print(f"\n✓ Sync OK: {len(updates)} actualizados, {len(nuevos)} nuevos. {round(time.time()-t0)}s")

if __name__ == "__main__":
    main()
