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
import os, json, urllib.request, urllib.parse, time, re

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

def _ncode(v):
    # Normaliza el código de ISPcube para comparar/matchear: los numéricos se rellenan
    # a 6 dígitos con ceros ("6477" -> "006477") para que un código cargado a mano sin
    # ceros no se trate como distinto y el sync NO lo marque 'eliminado' por error.
    s = str(v or "").strip()
    return s.zfill(6) if s.isdigit() else s

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
        chunk = None
        for attempt in range(4):   # reintentos por página: un 403/429/5xx transitorio no debe abortar ni devolver lista parcial
            try:
                d = json.load(_req(BASE + f"/customers/customers_list?limit={PAGE}&offset={offset}", headers=H))
                chunk = d if isinstance(d, list) else (d.get("data") or d.get("customers") or [])
                break
            except Exception as e:
                if attempt < 3:
                    time.sleep(5); continue
                # falló definitivamente → abortar TODO el sync: con la lista incompleta NO se puede marcar eliminados sin riesgo
                raise RuntimeError(f"isp_list_all: la página offset={offset} falló tras 4 intentos ({e}) — se aborta el sync para no borrar de más")
        calls += 1
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

def sb_insert_one(path, body):
    r = _req(SB_URL + "/rest/v1/" + path, body, "POST",
             {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Prefer": "return=representation"})
    d = json.load(r)
    return d[0] if isinstance(d, list) and d else None

def fetch_nexa():
    out, page = {}, 0
    while True:
        r = urllib.request.Request(
            SB_URL + "/rest/v1/clientes?select=id,codigo_ispcube,doc_numero,estado,deuda,caja_nap,puerto,precinto,portal_password,bloqueado_desde&codigo_ispcube=not.is.null&order=id.asc",
            headers=sb_headers({"Range": f"{page*1000}-{page*1000+999}"}))
        chunk = json.load(urllib.request.urlopen(r, timeout=60))
        for c in chunk: out[_ncode(c["codigo_ispcube"])] = c
        if len(chunk) < 1000: break
        page += 1
    return out

# DNIs comodín de ISPcube (sin documento real) → NO sirven para matchear/deduplicar
_DNI_PLACEHOLDER = {"9999999", "99999999", "0", "00000000", "11111111", "12345678"}
def _dni(s):
    d = re.sub(r"\D", "", str(s or "")) or None
    if not d: return None
    if d in _DNI_PLACEHOLDER: return None          # comodín → tratar como "sin DNI"
    if len(d) < 7 or len(set(d)) == 1: return None  # muy corto o todos iguales (0000000) → no confiable
    return d

def fetch_prospectos():
    # Prospectos/clientes SIN código ISPcube pero CON DNI → indexados por DNI.
    # Sirve para "graduar" (vincular) en vez de insertar un duplicado de la persona.
    out, page = {}, 0
    while True:
        r = urllib.request.Request(
            SB_URL + "/rest/v1/clientes?select=id,doc_numero,nombre,domicilio_full,plan_id&codigo_ispcube=is.null&doc_numero=not.is.null&order=id.asc",
            headers=sb_headers({"Range": f"{page*1000}-{page*1000+999}"}))
        chunk = json.load(urllib.request.urlopen(r, timeout=60))
        for c in chunk:
            k = _dni(c.get("doc_numero"))
            if k and k not in out: out[k] = c   # si hay varios con el mismo DNI, el primero
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
    code = _ncode(c.get("code"))
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
        "portal_password": c.get("portal_password"),
        **conex(c, portmap),
    }

def _f(v):
    try: return float(v)
    except: return None

def pull_tickets(token, isp=None):
    # Trae los tickets que los CLIENTES crean en ISPcube (customer_created) a la tabla isp_tickets,
    # para generar OTs desde Nexa. Importa solo los NUEVOS (no pisa los ya convertidos/descartados).
    # Saltea los HUÉRFANOS: tickets de clientes que ya NO existen en ISPcube (borrados) → no se puede
    # generar OT y solo ensucian la ticketera. Se usa la lista de clientes ya cargada (costo 0).
    vivos = {str(c.get("code") or "").strip() for c in (isp or []) if c.get("code")}
    H = {"api-key": APIKEY, "client-id": CLIENTID, "login-type": "api",
         "username": USER, "Authorization": "Bearer " + token}
    arr, off = [], 0
    while True:
        try:
            d = json.load(_req(BASE + f"/tickets/tickets_list?closed=0&limit=500&offset={off}", headers=H))
        except Exception as e:
            print("WARN pull_tickets:", e); break
        ch = d if isinstance(d, list) else (d.get("data") or d.get("tickets") or [])
        arr += ch
        if len(ch) < 500: break
        off += 500
    rows, huerfanos = [], 0
    for t in arr:
        if not t.get("customer_created"): continue   # solo los que crea el cliente
        c = t.get("customer") or {}
        code = str(c.get("code") or "").strip()
        if vivos and code and code not in vivos:      # cliente borrado de ISPcube → ticket huérfano, no importar
            huerfanos += 1; continue
        det = " | ".join([i.get("content", "") for i in (t.get("items") or []) if i.get("content")])[:2000]
        rows.append({
            "ticket_id": t.get("id"), "customer_id_isp": t.get("customer_id"),
            "codigo_ispcube": (str(c.get("code") or "").strip() or None),
            "cliente_nombre": c.get("name"), "doc_numero": c.get("doc_number"),
            "domicilio": t.get("address") or c.get("tax_residence"),
            "categoria": (t.get("ticket_category") or {}).get("name"), "categoria_id": t.get("ticket_category_id"),
            "estado_isp": (t.get("ticket_status") or {}).get("name"), "estado_isp_id": t.get("ticket_status_id"),
            "prioridad": (t.get("ticket_priority") or {}).get("name"),
            "detalle": det or None, "creado_por_cliente": True,
            "created_at_isp": t.get("created_at"),
        })
    if DRY:
        print(f"🎫 Tickets ISPcube (DRY): {len(rows)} creados por cliente ({huerfanos} huérfanos salteados) (no escribo)"); return
    try:
        ya = sb_get("isp_tickets?select=ticket_id&limit=20000")
        existentes = {r["ticket_id"] for r in ya}
    except Exception:
        existentes = set()
    nuevos = [r for r in rows if r["ticket_id"] not in existentes]
    for i in range(0, len(nuevos), 200):
        try: sb_post("isp_tickets", nuevos[i:i+200])
        except Exception as e: print(f"  ERROR insert tickets lote {i}: {e}")
    print(f"🎫 Tickets ISPcube: {len(nuevos)} nuevos importados (de {len(rows)} creados por cliente; {huerfanos} huérfanos salteados)")

def close_tickets(token):
    # Cierra en ISPcube los tickets cuya OT en Nexa ya quedó FINALIZADA (cierra el círculo).
    # SEGURO: lee el ticket actual y lo reescribe con estado=Cerrado (3) preservando los demás campos.
    H = {"api-key": APIKEY, "client-id": CLIENTID, "login-type": "api",
         "username": USER, "Authorization": "Bearer " + token}
    try:
        conv = sb_get("isp_tickets?estado_nexa=eq.convertido&ot_id=not.is.null&isp_cerrado_at=is.null"
                      "&select=id,ticket_id,ot_id&limit=2000")
    except Exception as e:
        print("WARN close_tickets sb_get:", e); return
    if not conv:
        print("🎫 Tickets a cerrar en ISPcube: 0"); return
    # ¿cuáles de esas OT están FINALIZADA?
    ids = [str(t["ot_id"]) for t in conv if t.get("ot_id")]
    fin = set()
    for i in range(0, len(ids), 100):
        try:
            r = sb_get("ordenes_trabajo?id=in.(%s)&estado=eq.FINALIZADA&select=id" % ",".join(ids[i:i+100]))
            for o in r: fin.add(o["id"])
        except Exception: pass
    cerrados = 0
    for t in conv:
        if t["ot_id"] not in fin: continue
        if DRY: cerrados += 1; continue
        try:
            cur = json.load(_req(BASE + "/tickets?ticket_id=" + str(t["ticket_id"]), headers=H))
            cur = cur[0] if isinstance(cur, list) else (cur.get("data") or cur)
        except Exception as e:
            print(f"  ERROR leer ticket {t['ticket_id']}: {e}"); continue
        body = {
            "ticket_area_id": cur.get("ticket_area_id"),
            "ticket_category_id": cur.get("ticket_category_id"),
            "ticket_priority_id": cur.get("ticket_priority_id"),
            "ticket_status_id": 3,   # Cerrado
            "assigned_user_id": cur.get("assigned_user_id"),
            "connection_id": cur.get("connection_id"),
            "price": cur.get("price"),
            "visit_date": cur.get("visit_date"),
            "visit_time_start": cur.get("visit_time_start"),
            "visit_time_end": cur.get("visit_time_end"),
            "new_item_content": "Resuelto — trabajo finalizado (Nexa, OT #%s)" % t["ot_id"],
        }
        try:
            _req(BASE + "/ticket/" + str(t["ticket_id"]), body, "PUT", H)
        except Exception as e:
            print(f"  ERROR cerrar ticket {t['ticket_id']}: {e}"); continue
        try:
            sb_patch("isp_tickets?id=eq." + str(t["id"]),
                     {"estado_nexa": "cerrado", "isp_cerrado_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        except Exception as e:
            print(f"  WARN marcar ticket {t['id']} cerrado: {e}")
        cerrados += 1
    print(f"🎫 Tickets cerrados en ISPcube: {cerrados}" + (" [DRY]" if DRY else ""))

def push_gps(token, isp):
    # Empuja a ISPcube el GPS del cierre del técnico (lat_fin/lng_fin) de las OT ya validadas
    # que todavía no se enviaron. SOLO de acá en adelante (no backfill). BARATO: reusa la lista
    # `isp` ya traída (mapa código->customer_id, sin llamadas extra) y manda 1 POST por OT, una sola vez.
    H = {"api-key": APIKEY, "client-id": CLIENTID, "login-type": "api",
         "username": USER, "Authorization": "Bearer " + token}
    code2id = {str(c.get("code") or "").strip(): c.get("id") for c in isp if c.get("code")}
    try:
        pend = sb_get("ordenes_trabajo?estado=eq.FINALIZADA&lat_fin=not.is.null&isp_gps_push_at=is.null"
                      "&select=id,lat_fin,lng_fin,clientes(codigo_ispcube)&limit=500")
    except Exception as e:
        print("WARN push_gps sb_get:", e); return
    enviados, sinmap = 0, 0
    for o in pend:
        code = ((o.get("clientes") or {}).get("codigo_ispcube") or "").strip()
        cid = code2id.get(code)
        if not cid:
            sinmap += 1; continue
        if DRY:
            enviados += 1; continue
        try:
            r = _req(BASE + "/customers/geolocation",
                     {"customer_id": cid, "lat": o["lat_fin"], "lng": o["lng_fin"]}, "POST", H)
            json.load(r)
        except Exception as e:
            print(f"  ERROR push_gps OT {o['id']}: {e}"); continue
        try:
            sb_patch("ordenes_trabajo?id=eq." + str(o["id"]),
                     {"isp_gps_push_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        except Exception as e:
            print(f"  WARN marcar OT {o['id']}: {e}")
        enviados += 1
    print(f"📍 GPS a ISPcube: {enviados} enviado(s)"
          + (f", {sinmap} sin código ISP (omitidos)" if sinmap else "")
          + (" [DRY]" if DRY else ""))

def generar_desconexiones_pendientes():
    # Genera órdenes PENDIENTE para clientes a dar de baja (baja ISPcube + bloqueado +30d) que no tengan ya una.
    # "Ya tiene" = desconexión ABIERTA (PEND/ASIG/PEND_ADMIN) + en revisión + finalizada.
    # NO cuenta ANULADA_PAGO/CANCELADA → el moroso que pagó y volvió a caer en mora vuelve a la cola.
    # Se chequea SOLO contra los candidatos (chunks), no toda la tabla (evita el tope de 1000 filas).
    try:
        cut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30*86400))
        # No generar baja para el que YA pagó (deuda<=0); null y >0 sí entran (puede no tener dato de deuda).
        baja = sb_get("clientes?estado=eq.baja&or=(deuda.gt.0,deuda.is.null)&select=id&limit=3000") or []
        blo  = sb_get("clientes?estado=eq.bloqueado&bloqueado_desde=lt.%s&or=(deuda.gt.0,deuda.is.null)&select=id&limit=3000" % cut) or []
        # Excluir: (a) clientes con INSTALACION abierta (prospectos aun NO instalados) y
        #          (b) clientes con un RETIRO ya FINALIZADO por OT (retiro hecho a mano) -> no duplicar en el pool.
        ret60 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60*86400))
        por_instalar = set()
        for x in (sb_get("ordenes_trabajo?tipo=eq.INSTALACION&estado=in.(PENDIENTE,ASIGNADA,EN_CURSO,CERRADA_TECNICO)&cliente_id=not.is.null&select=cliente_id&limit=5000") or []):
            if x.get("cliente_id"): por_instalar.add(x["cliente_id"])
        for x in (sb_get("ordenes_trabajo?tipo=in.(RETIRO_EQUIPOS,DESCONEXION)&estado=eq.FINALIZADA&fin_tec_at=gte.%s&cliente_id=not.is.null&select=cliente_id&limit=5000" % ret60) or []):
            if x.get("cliente_id"): por_instalar.add(x["cliente_id"])
        cand_ids = [c["id"] for c in baja if c["id"] not in por_instalar] + [c["id"] for c in blo if c["id"] not in por_instalar]
        yaset = set()
        for i in range(0, len(cand_ids), 150):
            part = ",".join(str(x) for x in cand_ids[i:i+150])
            r = sb_get("desconexiones?cliente_id=in.(%s)&estado=in.(PENDIENTE,ASIGNADA,PENDIENTE_ADMIN,EN_REVISION,FINALIZADO)&select=cliente_id&limit=2000" % part)
            for x in (r or []): yaset.add(x["cliente_id"])
    except Exception as e:
        print("WARN generar_desconexiones:", e); return 0
    nuevos  = [{"cliente_id": c["id"], "estado": "PENDIENTE", "origen": "baja_isp"}      for c in baja if c["id"] not in yaset and c["id"] not in por_instalar]
    nuevos += [{"cliente_id": c["id"], "estado": "PENDIENTE", "origen": "bloqueado_30d"} for c in blo  if c["id"] not in yaset and c["id"] not in por_instalar]
    if not nuevos: return 0
    if DRY:
        print("🔌 Desconexiones a auto-generar (DRY): %d" % len(nuevos)); return len(nuevos)
    n = 0
    for i in range(0, len(nuevos), 200):
        try: sb_post("desconexiones", nuevos[i:i+200]); n += len(nuevos[i:i+200])
        except Exception as e: print("  ERROR generar desconexiones lote %d: %s" % (i, e))
    return n

def anular_desconexiones_rehabilitados():
    # El cliente que PAGÓ = deuda <= 0 (NO basta estado 'activo': en ISPcube puede figurar habilitado
    # por un compromiso de pago aunque deba). Se cierran las bajas automáticas (baja_isp/bloqueado_30d)
    # y los compromisos de pago cuyo cliente quedó con deuda 0. Las MANUALES no se tocan.
    try:
        d  = sb_get("desconexiones?estado=in.(PENDIENTE,ASIGNADA)&origen=in.(baja_isp,bloqueado_30d)&select=id,clientes!inner(deuda)&clientes.deuda=lte.0&limit=2000") or []
        d += sb_get("desconexiones?estado=eq.COMPROMISO_PAGO&select=id,clientes!inner(deuda)&clientes.deuda=lte.0&limit=2000") or []
    except Exception as e:
        print("WARN anular_desconexiones:", e); return 0
    d = d if isinstance(d, list) else []
    if not d: return 0
    if DRY:
        print(f"🟢 Desconexiones a cerrar por pago (deuda 0) (DRY): {len(d)}"); return len(d)
    n = 0
    for o in d:
        try:
            sb_patch("desconexiones?id=eq." + str(o["id"]),
                     {"estado": "ANULADA_PAGO",
                      "resolucion": "El cliente pagó (deuda 0) — cerrada por el sync",
                      "resuelto_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "compromiso_vence": None})
            n += 1
        except Exception as e: print(f"  ERROR anular desconexion {o['id']}: {e}")
    return n

def limpiar_desconexiones_eliminados():
    # Bajas abiertas cuyo cliente ya fue ELIMINADO de ISPcube (corte/baja hecho fuera del sistema) → se cierran.
    try:
        d = sb_get("desconexiones?estado=in.(PENDIENTE,ASIGNADA,PENDIENTE_ADMIN)&select=id,clientes!inner(estado)&clientes.estado=eq.eliminado&limit=2000")
    except Exception as e:
        print("WARN limpiar_desconexiones_eliminados:", e); return 0
    d = d if isinstance(d, list) else []
    if not d: return 0
    if DRY:
        print(f"🗑️ Desconexiones a cerrar por cliente eliminado (DRY): {len(d)}"); return len(d)
    n = 0
    for o in d:
        try:
            sb_patch("desconexiones?id=eq." + str(o["id"]),
                     {"estado": "CANCELADA",
                      "resolucion": "Cliente ya eliminado de ISPcube (baja hecha fuera del sistema)",
                      "resuelto_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            n += 1
        except Exception as e: print(f"  ERROR cerrar desconexion eliminado {o['id']}: {e}")
    return n

def cerrar_desconexiones_por_retiro():
    # El RETIRO se hizo con una OT de retiro FINALIZADA → la baja PASA A PENDIENTE_ADMIN (no se cancela)
    # para que siga el circuito deposito -> soporte -> comercial. Avanza las PENDIENTE/ASIGNADA y crea una
    # en PENDIENTE_ADMIN si el cliente no tenia ninguna. Deja quietas las que ya estan en el circuito o resueltas.
    try:
        ret60 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60*86400))
        ots = sb_get("ordenes_trabajo?tipo=in.(RETIRO_EQUIPOS,DESCONEXION)&estado=eq.FINALIZADA&fin_tec_at=gte.%s&cliente_id=not.is.null&select=cliente_id,numero,fin_tec_at,cuadrilla_id&order=fin_tec_at.desc&limit=5000" % ret60) or []
        por_cli = {}
        for o in ots:
            if o.get("cliente_id") and o["cliente_id"] not in por_cli:
                por_cli[o["cliente_id"]] = o  # el retiro mas reciente por cliente
        ids = list(por_cli.keys())
        dx_por_cli = {}
        for i in range(0, len(ids), 150):
            part = ",".join(str(x) for x in ids[i:i+150])
            r = sb_get("desconexiones?cliente_id=in.(%s)&select=id,cliente_id,estado&limit=3000" % part) or []
            for x in r:
                dx_por_cli.setdefault(x["cliente_id"], []).append(x)
    except Exception as e:
        print("WARN cerrar_desconexiones_por_retiro:", e); return 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    avanzar, crear = [], []
    for cid, ot in por_cli.items():
        dxs = dx_por_cli.get(cid, [])
        body = {"estado": "PENDIENTE_ADMIN", "corte_at": ot.get("fin_tec_at") or now,
                "cuadrilla_id": ot.get("cuadrilla_id"), "obs_tecnico": "Corte/retiro hecho por OT N %s" % ot.get("numero"),
                "compromiso_vence": None}
        abierta = next((x for x in dxs if x["estado"] in ("PENDIENTE", "ASIGNADA")), None)
        en_flujo_o_resuelta = any(x["estado"] in ("PENDIENTE_ADMIN", "EN_REVISION", "FINALIZADO", "CANCELADA", "ANULADA_PAGO") for x in dxs)
        if abierta:
            avanzar.append((abierta["id"], body))
        elif not en_flujo_o_resuelta:
            crear.append(dict(body, cliente_id=cid, origen="baja_isp"))
    if not avanzar and not crear: return 0
    if DRY:
        print(f"📦 Retiros por OT → Pendiente admin (DRY): {len(avanzar)} avanzar, {len(crear)} crear"); return len(avanzar) + len(crear)
    n = 0
    for did, body in avanzar:
        try: sb_patch("desconexiones?id=eq." + str(did), body); n += 1
        except Exception as e: print(f"  ERROR avanzar desconexion retiro {did}: {e}")
    for i in range(0, len(crear), 200):
        try: sb_post("desconexiones", crear[i:i+200]); n += len(crear[i:i+200])
        except Exception as e: print(f"  ERROR crear desconexion retiro lote {i}: {e}")
    return n

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
    # registro de la corrida (para progreso + última/próxima en el panel)
    log_id = None
    if not DRY:
        try: row = sb_insert_one("sync_log", {"estado": "corriendo"}); log_id = row and row.get("id")
        except Exception as e: print("WARN sync_log insert:", e)
    token = isp_login()
    isp = isp_list_all(token)
    portmap = isp_ftthboxes(token)
    nexa = fetch_nexa()
    prospectos = fetch_prospectos()   # por DNI, para graduar en vez de duplicar
    planes = plan_map(); bemap = barrio_emp_map()
    # DNI (real) -> código de los clientes Nexa que YA tienen código → para NO insertar un duplicado de la misma persona
    dni_con_codigo = {}
    for code, c in nexa.items():
        k = _dni(c.get("doc_numero"))
        if k and k not in dni_con_codigo: dni_con_codigo[k] = code
    print(f"Nexa: {len(nexa)} clientes con codigo_ispcube · {len(prospectos)} prospectos sin código (con DNI)")

    updates, graduados, nuevos, isp_codes, evitados = [], [], [], set(), []
    for c in isp:
        code = _ncode(c.get("code"))
        if not code: continue
        isp_codes.add(code)
        est = STATUS_MAP.get(c.get("status"))
        deu = _f(c.get("duedebt"))
        if code in nexa:
            cur = nexa[code]; upd = {}
            # auto-corrige el código guardado torcido (ej "6477" -> "006477") para que
            # quede uniforme y no vuelva a fallar el matcheo / las búsquedas del panel
            if str(cur.get("codigo_ispcube") or "") != code: upd["codigo_ispcube"] = code
            if est and est != cur["estado"]: upd["estado"] = est
            # fecha REAL de bloqueo desde ISPcube (block_date) → vía "bloqueado +30d" de desconexiones
            if est == "bloqueado":
                bd = c.get("block_date")
                bd_iso = (str(bd)[:19].replace(" ", "T") + "Z") if bd else None
                if bd_iso and bd_iso != (cur.get("bloqueado_desde") or ""): upd["bloqueado_desde"] = bd_iso
            elif est == "activo" and cur.get("bloqueado_desde"):
                upd["bloqueado_desde"] = None
            if cur["estado"] == "eliminado": upd["eliminado_isp_at"] = None   # reapareció en ISPcube → revivir
            if deu is not None and float(deu) != float(cur.get("deuda") or 0): upd["deuda"] = deu
            cx = conex(c, portmap)
            for f in ("caja_nap", "puerto", "precinto"):
                if cx[f] and cx[f] != (cur.get(f) or None): upd[f] = cx[f]
            pp = c.get("portal_password")
            if pp and pp != (cur.get("portal_password") or None): upd["portal_password"] = pp
            if upd: updates.append((cur["id"], upd))
        else:
            # ¿hay un prospecto en Nexa con el mismo DNI? → graduarlo (vincular), no duplicar
            dni = _dni(c.get("doc_number"))
            pros = prospectos.pop(dni, None) if dni else None
            if pros:
                cx = conex(c, portmap)
                upd = {"codigo_ispcube": code, "estado": est or "activo"}
                if deu is not None: upd["deuda"] = deu
                for f in ("caja_nap", "puerto", "precinto"):
                    if cx[f]: upd[f] = cx[f]
                if c.get("portal_password"): upd["portal_password"] = c.get("portal_password")
                # completa sólo lo que el prospecto tenga vacío (no pisa lo cargado a mano)
                if not pros.get("nombre"): upd["nombre"] = c.get("name")
                if not pros.get("domicilio_full"): upd["domicilio_full"] = c.get("address") or c.get("tax_residence")
                if not pros.get("plan_id"): upd["plan_id"] = planes.get(c.get("plan_name"))
                graduados.append((pros["id"], upd))
            elif dni and dni in dni_con_codigo:
                # Ya existe en Nexa un cliente con ese DNI y código → NO duplico (ISPcube tiene 2 registros de la persona).
                evitados.append((code, dni_con_codigo[dni]))
            else:
                nuevos.append(nuevo_cliente(c, planes, bemap, portmap))

    print(f"\nA actualizar: {len(updates)} · a GRADUAR (prospecto→cliente por DNI): {len(graduados)} · NUEVOS a insertar: {len(nuevos)} · duplicados EVITADOS: {len(evitados)}")
    for g in graduados[:20]:
        print(f"  GRADÚA prospecto id={g[0]} → código {g[1]['codigo_ispcube']}")
    for n in nuevos[:20]:
        print(f"  NUEVO {n['codigo_ispcube']} {n['nombre']} ({n['estado']})")
    if evitados:
        print(f"  ⚠️ NO insertados (la persona ya existe en Nexa con otro código — duplicado en ISPcube a depurar):")
        for code, ya in evitados[:20]: print(f"     ISPcube {code} ≈ ya cargado como {ya}")
    # Posibles duplicados YA existentes en Nexa: prospecto sin código cuyo DNI ya tiene un cliente con código (revisar/fusionar a mano)
    revisar = [(p["id"], d, dni_con_codigo[d]) for d, p in prospectos.items() if d in dni_con_codigo]
    if revisar:
        print(f"  ⚠️ PROSPECTOS que parecen duplicados (mismo DNI que un cliente ya con código) — revisar/fusionar:")
        for pid, d, code in revisar[:20]: print(f"     prospecto id={pid} (DNI {d}) ≈ cliente código {code}")

    # Push del GPS de los cierres validados a ISPcube (reusa token+lista; respeta DRY adentro)
    push_gps(token, isp)
    # Importar tickets que los clientes crearon en ISPcube → tabla isp_tickets (para generar OTs)
    pull_tickets(token, isp)
    # Cerrar en ISPcube los tickets cuya OT ya quedó finalizada (cierra el círculo)
    close_tickets(token)

    if DRY:
        print("\n[DRY] No se escribió nada.")
        return
    for cid, upd in updates:
        try: sb_patch("clientes?id=eq." + str(cid), upd)
        except Exception as e: print(f"  ERROR upd {cid}: {e}")
    for cid, upd in graduados:
        try: sb_patch("clientes?id=eq." + str(cid), upd)
        except Exception as e: print(f"  ERROR graduar {cid}: {e}")
    # nuevos en lotes de 200 (claves homogéneas)
    for i in range(0, len(nuevos), 200):
        try: sb_post("clientes", nuevos[i:i+200])
        except Exception as e: print(f"  ERROR insert lote {i}: {e}")

    # ELIMINADOS: clientes en Nexa con código que YA NO están en ISPcube (Gastón los borró).
    # Se marcan estado='eliminado' (no se muestran ni cuentan; queda el registro p/ avisar por DNI).
    # SEGURO: solo si la lista de ISPcube vino completa (evita borrado masivo por falla de API).
    eliminados = 0
    huerfanos = [(cur["id"]) for code, cur in nexa.items()
                 if code not in isp_codes and cur.get("estado") != "eliminado"]
    if len(isp_codes) < 0.6 * len(nexa):
        print(f"\n⚠️ ISPcube devolvió {len(isp_codes)} de ~{len(nexa)} esperados — lista incompleta, NO marco eliminados (seguro).")
    else:
        for cid in huerfanos:
            try:
                sb_patch("clientes?id=eq." + str(cid),
                         {"estado": "eliminado", "eliminado_isp_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                eliminados += 1
            except Exception as e: print(f"  ERROR eliminar {cid}: {e}")
        if eliminados: print(f"\n🗑️ Marcados 'eliminado' (borrados de ISPcube): {eliminados}")

    # Desconexiones: limpiar las de clientes eliminados, cerrar las cuyo retiro ya se hizo por OT,
    # auto-generar las pendientes y auto-anular las de rehabilitados (pagaron)
    limpiadas = limpiar_desconexiones_eliminados()
    cerradas_retiro = cerrar_desconexiones_por_retiro()
    generadas = generar_desconexiones_pendientes()
    anuladas = anular_desconexiones_rehabilitados()

    print(f"\n✓ Sync OK: {len(updates)} actualizados, {len(graduados)} graduados, {len(nuevos)} nuevos, {eliminados} eliminados, {generadas} desconexiones generadas, {anuladas} anuladas, {limpiadas} cerradas por eliminado, {cerradas_retiro} cerradas por retiro. {round(time.time()-t0)}s")
    if log_id:
        try: sb_patch("sync_log?id=eq." + str(log_id),
                      {"estado": "ok", "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "isp_total": len(isp_codes), "actualizados": len(updates), "nuevos": len(nuevos),
                       "graduados": len(graduados), "eliminados": eliminados})
        except Exception as e: print("WARN sync_log update:", e)

if __name__ == "__main__":
    main()
