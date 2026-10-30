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
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
HERE = Path(__file__).resolve().parent
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
RAILS = {"state": "idle", "error": None, "raw": None, "gz": None, "built": None, "tiles": "0/0"}
LAT_CUTS = [35.9, 38.6, 41.3, 44.0]
LON_CUTS = [-9.7, -7.06, -4.42, -1.78, 0.86, 3.5]
EPS = 0.0002  # ~22 m


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


def build_rails():
    path = CACHE_DIR / "rails.json"
    try:
        if path.exists():
            data = json.loads(path.read_text())
            fresh = time.time() - path.stat().st_mtime
            if (data.get("complete") and fresh < 45 * 86400) or fresh < 6 * 3600:
                _set_rails(data)
                log("Vías cargadas de la caché (%d tramos)" % (len(data["std"]) + len(data["hs"])))
                if data.get("complete"):
                    return
        RAILS["state"] = "building"
        seen, hs, std = set(), [], []
        tiles = [(LAT_CUTS[i], LON_CUTS[j], LAT_CUTS[i + 1], LON_CUTS[j + 1])
                 for i in range(len(LAT_CUTS) - 1) for j in range(len(LON_CUTS) - 1)]
        failed = 0
        for n, (s, w, nn, e) in enumerate(tiles, 1):
            RAILS["tiles"] = "%d/%d" % (n - 1, len(tiles)); RAILS["error"] = None if not failed else RAILS["error"]
            q = ('[out:json][timeout:180];'
                 'way["railway"~"^(rail|narrow_gauge)$"]["usage"~"^(main|branch)$"]["service"!~"."]'
                 '(%s,%s,%s,%s);out geom qt;' % (s, w, nn, e))
            log("Vías: descargando zona %d/%d…" % (n, len(tiles)))
            try:
                j = overpass(q)
            except Exception as ex:  # noqa: BLE001
                failed += 1
                RAILS["error"] = str(ex)
                continue
            for el in j.get("elements", []):
                if el.get("id") in seen or not el.get("geometry"):
                    continue
                seen.add(el["id"])
                pts = rdp([(p["lat"], p["lon"]) for p in el["geometry"]], EPS)
                pts = [[round(a, 5), round(b, 5)] for a, b in pts]
                (hs if (el.get("tags") or {}).get("highspeed") == "yes" else std).append(pts)
            time.sleep(5)
        data = {"complete": failed == 0, "built": int(time.time()), "hs": hs, "std": std}
        if hs or std:
            path.write_text(json.dumps(data, separators=(",", ":")))
            _set_rails(data)
            log("Vías listas: %d tramos (%d zonas fallidas)" % (len(hs) + len(std), failed))
        else:
            RAILS["state"] = "error"
    except Exception as e:  # noqa: BLE001
        RAILS["state"], RAILS["error"] = "error", str(e)
        log("Vías: error %s" % e)


def _set_rails(data):
    raw = json.dumps(data, separators=(",", ":")).encode()
    RAILS.update(raw=raw, gz=gzip.compress(raw, 6), state="ready", built=data.get("built"))


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
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path in ("/api/cercanias", "/api/larga"):
            kind = path[5:]
            obj, err = get_json(VEHICLES[kind])
            if obj is None:
                return self._json(502, {"error": err})
            self._json(200, enrich(kind, json.loads(json.dumps(obj))))
        elif path == "/api/rails":
            if RAILS["state"] != "ready":
                return self._json(503, {"state": RAILS["state"], "tiles": RAILS["tiles"], "error": RAILS["error"]})
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                self._send(200, RAILS["gz"], "application/json", {"Content-Encoding": "gzip"})
            else:
                self._send(200, RAILS["raw"], "application/json")
        elif path == "/api/status":
            self._json(200, {"static": {k: STATIC[k] for k in ("ready", "error", "n_trips")},
                             "rails": {k: RAILS[k] for k in ("state", "error", "built", "tiles")}})
        else:
            self._send(404, b"No encontrado", "text/plain; charset=utf-8")


if __name__ == "__main__":
    threading.Thread(target=load_static, daemon=True).start()
    threading.Thread(target=build_rails, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Monitor de trenes RENFE en http://127.0.0.1:%d  (Ctrl+C para salir)" % PORT)
    print("La primera vez se descargan las vías y el GTFS (1–5 min); luego quedan en .cache/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nAdiós")
