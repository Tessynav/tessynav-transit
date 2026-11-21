"""MapKoon transit data builder (v1, Nov 20 2026), run by GitHub Actions every week.
Downloads STM's public schedule file (GTFS, CC BY 4.0, (c) Société de transport de Montréal) and writes small files
the app loads only near the car:
  data/shapes/<shape_id>.json   the exact road path of one bus route variant: [[lon,lat],...] (simplified to ~2 m)
  data/trips/<route_id>.json    for one route: {trip_id: [shape_id, headsign, direction]} (to know which path a live bus follows)
  data/stops/<tx>_<ty>.json     bus stops in a ~2 km square: [[stop_id, name, lat, lon, [route numbers]], ...]
  data/meta.json                when it was built, how many routes / shapes / stops, the tile size
Bus only (metro lines are left out: STM's terms don't allow building on metro schedules). Unofficial, no STM logo."""
import csv, io, json, math, os, sys, time, urllib.request, zipfile, collections, shutil

URL = 'https://www.stm.info/sites/default/files/gtfs/gtfs_stm.zip'
OUT = 'data'
TILE = 0.02   # degrees (about 2.2 km north-south, 1.6 km east-west in Montréal)

def rows(z, name):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig'))

def simplify(pts, tol=2.0):
    """Douglas-Peucker in metres (pts: [(lon,lat)])"""
    if len(pts) < 3: return pts
    lat0 = math.radians(pts[0][1]); kx = 111320 * math.cos(lat0); ky = 110540
    P = [((p[0] - pts[0][0]) * kx, (p[1] - pts[0][1]) * ky) for p in pts]
    keep = [False] * len(P); keep[0] = keep[-1] = True; st = [(0, len(P) - 1)]
    while st:
        a, b = st.pop(); ax, ay = P[a]; bx, by = P[b]; dx, dy = bx - ax, by - ay; L = math.hypot(dx, dy) or 1e-9
        best, bi = 0, -1
        for i in range(a + 1, b):
            d = abs((P[i][0] - ax) * dy - (P[i][1] - ay) * dx) / L
            if d > best: best, bi = d, i
        if best > tol: keep[bi] = True; st += [(a, bi), (bi, b)]
    return [pts[i] for i in range(len(pts)) if keep[i]]

def main(src=None):
    t0 = time.time()
    if src: data = open(src, 'rb').read()
    else:
        req = urllib.request.Request(URL, headers={'User-Agent': 'MapKoon transit builder'})
        data = urllib.request.urlopen(req, timeout=300).read()
    z = zipfile.ZipFile(io.BytesIO(data))
    if os.path.isdir(OUT): shutil.rmtree(OUT)
    for d in ('shapes', 'trips', 'stops'): os.makedirs(os.path.join(OUT, d))
    # bus routes only (route_type 3)
    routes = {r['route_id']: (r.get('route_short_name') or r['route_id']) for r in rows(z, 'routes.txt') if r.get('route_type', '3') == '3'}
    # trips: which path each trip follows
    trips = collections.defaultdict(dict); used = set()
    for r in rows(z, 'trips.txt'):
        if r['route_id'] not in routes or not r.get('shape_id'): continue
        trips[r['route_id']][r['trip_id']] = [r['shape_id'], r.get('trip_headsign', ''), int(r.get('direction_id') or 0)]
        used.add(r['shape_id'])
    for rid, T in trips.items():
        with open(os.path.join(OUT, 'trips', rid + '.json'), 'w') as f: json.dump(T, f, separators=(',', ':'), ensure_ascii=False)
    # shapes: the road path, in order
    sh = collections.defaultdict(list)
    for r in rows(z, 'shapes.txt'):
        if r['shape_id'] in used: sh[r['shape_id']].append((int(r['shape_pt_sequence']), float(r['shape_pt_lon']), float(r['shape_pt_lat'])))
    for sid, P in sh.items():
        P.sort(); pts = simplify([(p[1], p[2]) for p in P])
        with open(os.path.join(OUT, 'shapes', sid + '.json'), 'w') as f: json.dump([[round(x, 6), round(y, 6)] for x, y in pts], f, separators=(',', ':'))
    # stops served by buses, with their route numbers
    trip_route = {tid: rid for rid, T in trips.items() for tid in T}
    stop_routes = collections.defaultdict(set)
    for r in rows(z, 'stop_times.txt'):
        rid = trip_route.get(r['trip_id'])
        if rid: stop_routes[r['stop_id']].add(routes[rid])
    tiles = collections.defaultdict(list); ns = 0
    for r in rows(z, 'stops.txt'):
        sid = r['stop_id']
        if sid not in stop_routes: continue
        la, lo = float(r['stop_lat']), float(r['stop_lon'])
        key = '%d_%d' % (math.floor(lo / TILE), math.floor(la / TILE))
        tiles[key].append([sid, r.get('stop_name', ''), round(la, 6), round(lo, 6), sorted(stop_routes[sid], key=lambda s: (len(s), s))]); ns += 1
    for k, L in tiles.items():
        with open(os.path.join(OUT, 'stops', k + '.json'), 'w') as f: json.dump(L, f, separators=(',', ':'), ensure_ascii=False)
    meta = {'built': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'routes': len(trips), 'shapes': len(sh), 'stops': ns, 'tile': TILE,
            'source': 'STM GTFS (CC BY 4.0), unofficial', 'seconds': round(time.time() - t0)}
    with open(os.path.join(OUT, 'meta.json'), 'w') as f: json.dump(meta, f)
    print(json.dumps(meta))

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else None)
