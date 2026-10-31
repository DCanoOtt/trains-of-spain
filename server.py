#!/usr/bin/env python3
"""
Monitor de trenes RENFE - servidor local (solo librería estándar, sin dependencias).

Qué hace:
  * Descarga los feeds abiertos de Renfe (GTFS-Realtime en JSON): posiciones y retrasos.
  * Descarga el GTFS estático de Alta Velocidad / Larga / Media Distancia para saber el
    TIPO de cada tren (AVE, AVLO, Alvia, Euromed, Intercity, Avant, MD...) y el destino.
  * Construye (una sola vez, con caché en disco) el trazado de las vías de la península
    a partir de OpenStreetMap (Overpass) y lo sirve ya simplificado.
  * Sirve index.html y una pequeña API para el navegador.

Uso:   python3 server.py [puerto]      (por defecto 8765)
Abrir: http://127.0.0.1:8765
"""
import csv
import datetime
import gzip
import io
import json
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Certificados HTTPS. Orden: variable SSL_CERT_FILE -> almacén del sistema (truststore: reconoce los
# certificados corporativos/antivirus que reemplazan los de la web) -> certifi -> valores por defecto.
# Último recurso, solo para datos públicos de Renfe: --insecure (o RENFE_INSECURE=1) no verifica certificados.
import os


def _make_ctx():
    if "--insecure" in sys.argv or os.environ.get("RENFE_INSECURE") == "1":
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        print("AVISO: verificación de certificados desactivada (--insecure)")
        return c
    if os.environ.get("SSL_CERT_FILE"):
        return ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_ctx = _make_ctx()
urllib.request.install_opener(urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ctx)))

_args = [a for a in sys.argv[1:] if not a.startswith("--")]
PORT = int(_args[0]) if _args else 8765
FROZEN = getattr(sys, "frozen", False)          # True dentro del ejecutable (PyInstaller)
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))   # recursos empaquetados
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent  # datos junto al ejecutable
CACHE_DIR = HERE / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
FEED_TTL = 15  # segundos

RT = "https://gtfsrt.renfe.com/"
VEHICLES = {"cercanias": RT + "vehicle_positions.json", "larga": RT + "vehicle_positions_LD.json"}
TRIPS = {"cercanias": RT + "trip_updates.json", "larga": RT + "trip_updates_LD.json"}
GTFS_LD = "https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter"]
UA = {"User-Agent": "renfe-trenes-monitor/1.0 (uso personal)"}


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={**UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- feeds en tiempo real
_cache = {}
_lock = threading.Lock()


def get_json(url, ttl=FEED_TTL):
    """Devuelve (objeto, aviso). Caché de ttl segundos; si falla, último dato bueno."""
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit["t"] < ttl:
            return hit["obj"], None
    try:
        obj = json.loads(http_get(url, 25))
    except Exception as e:  # noqa: BLE001
        with _lock:
            stale = _cache.get(url)
        if stale:
            return stale["obj"], "dato en caché (%s)" % e
        return None, str(e)
    with _lock:
        _cache[url] = {"t": now, "obj": obj}
    return obj, None


def pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def num_key(s):
    """'08043' / '8043_20261030' -> '8043' (primer grupo de dígitos sin ceros a la izquierda)."""
    m = re.search(r"\d+", str(s or ""))
    return m.group(0).lstrip("0") or "0" if m else None


def trip_variants(tid):
    tid = str(tid or "")
    if tid:
        yield tid
        for sep in ("_", "-", "|", ":"):
            if sep in tid:
                yield tid.split(sep)[0]


# ---------------------------------------------------------------- GTFS estático (larga distancia)
STATIC = {"ready": False, "error": None, "trips": {}, "numbers": {}, "stops": {}, "n_trips": 0}


def load_static():
    path = CACHE_DIR / "ld_google_transit.zip"
    try:
        if not path.exists() or time.time() - path.stat().st_mtime > 24 * 3600:
            log("Descargando GTFS estático de larga distancia…")
            try:
                path.write_bytes(http_get(GTFS_LD, 180))
            except Exception as e:  # noqa: BLE001
                if not path.exists():
                    raise
                log("No se pudo actualizar el GTFS, uso la copia anterior: %s" % e)
        with zipfile.ZipFile(path) as z:
            def rows(name):
                with z.open(name) as f:
                    yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            routes = {r.get("route_id"): (r.get("route_short_name") or r.get("route_long_name") or "").strip()
                      for r in rows("routes.txt")}
            stops = {r.get("stop_id"): r.get("stop_name") for r in rows("stops.txt")}
            trips, numbers = {}, {}
            for t in rows("trips.txt"):
                prod = routes.get(t.get("route_id"), "")
                number = (t.get("trip_short_name") or "").strip()
                head = (t.get("trip_headsign") or "").strip()
                trips[t.get("trip_id")] = (prod, number, head)
                k = num_key(number)
                if k and k not in numbers:
                    numbers[k] = (prod, number, head)
        STATIC.update(trips=trips, numbers=numbers, stops=stops, ready=True, error=None, n_trips=len(trips))
        log("GTFS estático listo: %d viajes, %d paradas" % (len(trips), len(stops)))
    except Exception as e:  # noqa: BLE001
        STATIC["error"] = str(e)
        log("GTFS estático no disponible: %s" % e)


def lookup_meta(tid, label):
    for v in trip_variants(tid):
        if v in STATIC["trips"]:
            return STATIC["trips"][v]
    for src in (tid, label):
        k = num_key(src)
        if k and k in STATIC["numbers"]:
            return STATIC["numbers"][k]
    return None


# ---------------------------------------------------------------- retrasos (trip updates)
def choose(recs, veh_start):
    """Elige el registro de retraso/supresión que corresponde a la fecha de servicio del tren."""
    if not recs:
        return None
    if veh_start:
        for r in recs:
            if r["start"] == veh_start:
                return r
        return next((r for r in recs if not r["start"]), None)
    today = datetime.date.today()
    valid = {today.strftime("%Y%m%d"), (today - datetime.timedelta(days=1)).strftime("%Y%m%d")}
    ok = [r for r in recs if not r["start"] or r["start"] in valid]
    for r in ok:
        if r["start"] == today.strftime("%Y%m%d"):
            return r
    return ok[0] if ok else None


def delay_maps(kind):
    """(por tripId, por nº de tren, aviso). Retraso en segundos (negativo = adelanto)."""
    obj, err = get_json(TRIPS[kind])
    by_id, by_num = {}, {}
    if not obj:
        return by_id, by_num, err
    for e in obj.get("entity") or []:
        tu = pick(e, "tripUpdate", "trip_update")
        if not tu:
            continue
        trip = tu.get("trip") or {}
        tid = str(pick(trip, "tripId", "trip_id") or "")
        if not tid:
            continue
        rel = pick(trip, "scheduleRelationship", "schedule_relationship")
        start = str(pick(trip, "startDate", "start_date") or "")
        if rel in ("CANCELED", "CANCELLED", 3):
            rec = {"canceled": True, "info": "scheduleRelationship=%s, fecha de servicio=%s, tripId=%s" % (rel, start or "—", tid)}
        else:
            d = tu.get("delay")
            if d is None:
                for stu in pick(tu, "stopTimeUpdate", "stop_time_update") or []:
                    for k in ("arrival", "departure"):
                        x = stu.get(k)
                        if isinstance(x, dict) and x.get("delay") is not None:
                            d = x["delay"]
                            break
                    if d is not None:
                        break
            if d is None:
                continue
            rec = {"delay": int(d)}
        rec["start"] = start
        by_id.setdefault(tid, []).append(rec)
        if kind == "larga":
            k = num_key(tid)
            if k:
                by_num.setdefault(k, []).append(rec)
    return by_id, by_num, err


def enrich(kind, obj):
    """Añade a cada vehículo: _product, _number, _headsign, _stopName, _delay, _canceled."""
    by_id, by_num, derr = delay_maps(kind)
    n = typed = delayed = 0
    for e in obj.get("entity") or []:
        v = e.get("vehicle")
        if not isinstance(v, dict):
            continue
        n += 1
        trip = v.get("trip") or {}
        tid = str(pick(trip, "tripId", "trip_id") or "")
        label = str(pick(v.get("vehicle") or {}, "label", "id") or "")
        if kind == "larga" and STATIC["ready"]:
            m = lookup_meta(tid, label)
            if m:
                e["_product"], e["_number"], e["_headsign"] = m
                typed += 1
        sid = str(pick(v, "stopId", "stop_id") or "")
        if sid and sid in STATIC["stops"] and kind == "larga":
            e["_stopName"] = STATIC["stops"][sid]
        vstart = str(pick(trip, "startDate", "start_date") or "")
        rec = choose(by_id.get(tid), vstart)
        exact = rec is not None
        if rec is None and kind == "larga":  # en larga distancia el nº de tren identifica el viaje
            for var in trip_variants(tid):
                rec = choose(by_id.get(var), vstart)
                if rec:
                    break
            if rec is None:
                k = num_key(tid) or num_key(label)
                rec = choose(by_num.get(k), vstart) if k else None
        if rec and rec.get("canceled") and not exact:
            rec = None  # una supresión solo vale si es exactamente de este viaje (no de otro día con el mismo nº)
        if rec:
            delayed += 1
            if rec.get("canceled"):
                e["_canceled"] = True
                e["_cancelInfo"] = rec.get("info", "")
            else:
                e["_delay"] = rec["delay"]
    obj["_info"] = {"vehicles": n, "typed": typed, "delayed": delayed, "delayFeed": len(by_id),
                    "static": STATIC["ready"], "delayError": derr}
    return obj


# ---------------------------------------------------------------- trazado de vías (OSM / Overpass)
# El trazado se descarga una vez y se guarda en data/rails.json (se puede subir a GitHub para que nadie
# tenga que esperar). Se sirve por zona y con distinto nivel de detalle según el zoom, para que sea rápido.
DATA_DIR = HERE / "data"
RAILS = {"state": "idle", "error": None, "built": None, "tiles": "0/0", "complete": True, "levels": {}}
LAT_CUTS = [35.9, 38.6, 41.3, 44.0]
LON_CUTS = [-9.7, -7.06, -4.42, -1.78, 0.86, 3.5]
BASE_EPS = 0.0002                                  # ~22 m
LEVEL_EPS = {2: 0.0005, 1: 0.0015, 0: 0.006}       # simplificación de los niveles gruesos (zoom bajo)
FLAG_HS, FLAG_MAIN = 1, 2


def level_for_zoom(z):
    return 0 if z <= 6 else 1 if z <= 8 else 2 if z <= 10 else 3


def rdp(pts, eps):
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        dx, dy = pts[b][0] - ax, pts[b][1] - ay
        l2 = dx * dx + dy * dy
        dmax, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if l2 == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
                d = (px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2
            if d > dmax:
                dmax, idx = d, i
        if idx > 0 and dmax > eps * eps:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(pts, keep) if k]


def overpass(query):
    body = ("data=" + urllib.parse.quote(query)).encode()
    last = None
    for attempt in range(3):
        for url in OVERPASS:
            try:
                req = urllib.request.Request(url, data=body, headers={
                    **UA, "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    j = json.loads(r.read())
                # Overpass responde 200 con un "remark" cuando se queda sin tiempo/memoria: datos incompletos
                remark = str(j.get("remark", ""))
                if re.search(r"error|timed out|out of memory|too busy", remark, re.I):
                    raise RuntimeError(remark[:200])
                return j
            except Exception as e:  # noqa: BLE001
                last = e
                log("Overpass %s falló: %s" % (url, e))
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(last)


def _load_rails_file():
    """Devuelve datos {'complete','built','ways'} de data/rails.json (o de la caché antigua), o None."""
    for cand in (DATA_DIR / "rails.json", CACHE_DIR / "rails.json"):
        if not cand.exists():
            continue
        try:
            d = json.loads(cand.read_text())
        except Exception as e:  # noqa: BLE001
            log("No se pudo leer %s: %s" % (cand, e))
            continue
        if "ways" not in d:  # formato antiguo {hs, std}
            d["ways"] = [[FLAG_HS | FLAG_MAIN, p] for p in d.get("hs", [])] + [[FLAG_MAIN, p] for p in d.get("std", [])]
        if d.get("complete") or time.time() - cand.stat().st_mtime < 6 * 3600:
            return d
    return None


def build_rails():
    try:
        data = _load_rails_file()
        if data:
            _set_rails(data)
            log("Vías cargadas del disco (%d tramos)" % len(data["ways"]))
            if data.get("complete"):
                return
        RAILS["state"] = "building" if not data else "ready"
        tiles = [(LAT_CUTS[i], LON_CUTS[j], LAT_CUTS[i + 1], LON_CUTS[j + 1])
                 for i in range(len(LAT_CUTS) - 1) for j in range(len(LON_CUTS) - 1)]
        done = [0]
        lock = threading.Lock()

        def one(tile):
            s, w, n, e = tile
            q = ('[out:json][timeout:180];'
                 'way["railway"~"^(rail|narrow_gauge)$"]["usage"~"^(main|branch)$"]["service"!~"."]'
                 '(%s,%s,%s,%s);out geom qt;' % (s, w, n, e))
            try:
                j = overpass(q)
            except Exception as ex:  # noqa: BLE001
                RAILS["error"] = str(ex)
                return None
            out = []
            for el in j.get("elements", []):
                if not el.get("geometry"):
                    continue
                tags = el.get("tags") or {}
                flags = (FLAG_HS if tags.get("highspeed") == "yes" else 0) | (FLAG_MAIN if tags.get("usage") == "main" else 0)
                pts = rdp([(p["lat"], p["lon"]) for p in el["geometry"]], BASE_EPS)
                out.append((el.get("id"), flags, [[round(a, 5), round(b, 5)] for a, b in pts]))
            time.sleep(1)
            with lock:
                done[0] += 1
                RAILS["tiles"] = "%d/%d" % (done[0], len(tiles))
            log("Vías: zona %d/%d lista" % (done[0], len(tiles)))
            return out

        RAILS["tiles"] = "0/%d" % len(tiles)
        seen, ways, failed = set(), [], 0
        with ThreadPoolExecutor(max_workers=2) as ex:   # 2 consultas a la vez: respeta a Overpass y acelera
            for res in ex.map(one, tiles):
                if res is None:
                    failed += 1
                    continue
                for wid, flags, pts in res:
                    if wid in seen:
                        continue
                    seen.add(wid)
                    ways.append([flags, pts])
        data = {"complete": failed == 0, "built": int(time.time()), "ways": ways}
        if ways:
            DATA_DIR.mkdir(exist_ok=True)
            (DATA_DIR / "rails.json").write_text(json.dumps(data, separators=(",", ":")))
            _set_rails(data)
            log("Vías listas: %d tramos (%d zonas fallidas)" % (len(ways), failed))
        elif RAILS["state"] != "ready":
            RAILS["state"] = "error"
    except Exception as e:  # noqa: BLE001
        RAILS["error"] = str(e)
        if RAILS["state"] != "ready":
            RAILS["state"] = "error"
        log("Vías: error %s" % e)


def _set_rails(data):
    """Prepara los 4 niveles de detalle (3 = completo ... 0 = solo líneas principales, muy simplificado)."""
    ways = []
    for flags, pts in data["ways"]:
        if len(pts) < 2:
            continue
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        ways.append((flags, pts, (min(lats), min(lons), max(lats), max(lons))))
    levels = {3: ways}
    for lvl in (2, 1, 0):
        eps, out = LEVEL_EPS[lvl], []
        for flags, pts, bb in levels[lvl + 1]:
            if lvl <= 1 and not flags & (FLAG_HS | FLAG_MAIN):
                continue                                   # a zoom bajo, solo líneas principales
            if lvl <= 1 and max(bb[2] - bb[0], bb[3] - bb[1]) < eps * 2:
                continue                                   # tramos diminutos
            q = rdp(pts, eps)
            if len(q) >= 2:
                out.append((flags, q, bb))
        levels[lvl] = out
    RAILS.update(levels=levels, built=data.get("built"), complete=bool(data.get("complete")), state="ready")
    log("Niveles de detalle de las vías: " + ", ".join("%d→%d tramos" % (k, len(v)) for k, v in sorted(levels.items())))


def rails_query(z, bbox):
    lvl = level_for_zoom(z)
    hs, std = [], []
    for flags, pts, bb in RAILS["levels"].get(lvl, []):
        if bbox and (bb[2] < bbox[0] or bb[0] > bbox[2] or bb[3] < bbox[1] or bb[1] > bbox[3]):
            continue
        (hs if flags & FLAG_HS else std).append(pts)
    return {"level": lvl, "built": RAILS["built"], "complete": RAILS["complete"], "hs": hs, "std": std}


# ---------------------------------------------------------------- servidor HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, (RES / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path in ("/api/cercanias", "/api/larga"):
            kind = path[5:]
            obj, err = get_json(VEHICLES[kind])
            if obj is None:
                return self._json(502, {"error": err})
            self._json(200, enrich(kind, json.loads(json.dumps(obj))))
        elif path == "/api/rails":
            if RAILS["state"] != "ready":
                return self._json(503, {"state": RAILS["state"], "tiles": RAILS["tiles"], "error": RAILS["error"]})
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                z = int(float(qs.get("z", ["7"])[0]))
                bbox = [float(x) for x in qs["bbox"][0].split(",")] if "bbox" in qs else None
                if bbox and len(bbox) != 4:
                    bbox = None
            except ValueError:
                return self._json(400, {"error": "parámetros z/bbox no válidos"})
            body = json.dumps(rails_query(z, bbox), separators=(",", ":")).encode()
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                self._send(200, gzip.compress(body, 3), "application/json", {"Content-Encoding": "gzip"})
            else:
                self._send(200, body, "application/json")
        elif path == "/api/status":
            self._json(200, {"static": {k: STATIC[k] for k in ("ready", "error", "n_trips")},
                             "rails": {k: RAILS[k] for k in ("state", "error", "built", "tiles", "complete")}})
        else:
            self._send(404, b"No encontrado", "text/plain; charset=utf-8")


if __name__ == "__main__":
    # Primera vez en el ejecutable: copia las vías precalculadas incluidas dentro del paquete
    _seed = RES / "data" / "rails.json"
    if FROZEN and _seed.exists() and not (DATA_DIR / "rails.json").exists():
        DATA_DIR.mkdir(exist_ok=True)
        (DATA_DIR / "rails.json").write_bytes(_seed.read_bytes())
    if FROZEN or "--open" in sys.argv:
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    threading.Thread(target=load_static, daemon=True).start()
    threading.Thread(target=build_rails, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Monitor de trenes RENFE en http://127.0.0.1:%d  (Ctrl+C para salir)" % PORT)
    print("La primera vez se descargan las vías y el GTFS (1–5 min); luego quedan en .cache/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nAdiós")
