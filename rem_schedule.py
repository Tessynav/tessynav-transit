"""MapKoon REM schedule builder (v1, Nov 25 2026), run by GitHub Actions every day.
Downloads the REM's public schedule (GTFS) and writes small files so the app can place each REM train where the
timetable says it is right now, at its real speed between stations:

  rem/net.json                 {"shapes": {shape_id: [lon, lat, lon, lat, ...]}, "stops": [[name, lat, lon], ...]}
  rem/day/YYYYMMDD.json        {"t": [[shape_id, [[dist_m, arr_s, dep_s], ...]], ...]}  every trip running that service
                               day (times in seconds after midnight, Montréal time; past 24 h for trips after midnight)

The schedule file's address is looked up in the Mobility Database's open catalog (it moves from time to time);
REM_GTFS_URL in the environment overrides it. Unofficial, not presented as REM's own information.

  python3 rem_schedule.py [file.zip]
"""
import csv, datetime, io, json, math, os, sys, urllib.request, zipfile, collections

OUT = 'rem'   # (its own folder: the weekly bus job clears data/)
CATALOG = 'https://files.mobilitydatabase.org/feeds_v2.csv'
DAYS = 8
UA = {'User-Agent': 'MapKoon transit builder (github.com/Tessynav)'}


def get(url, timeout=300):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def find_url():
    if os.environ.get('REM_GTFS_URL'):
        return [os.environ['REM_GTFS_URL']]
    txt = get(CATALOG).decode('utf-8', 'replace')
    urls = []
    for r in csv.DictReader(io.StringIO(txt)):
        prov = (r.get('provider') or '') + ' ' + (r.get('feed_name') or '') + ' ' + (r.get('name') or '')
        if (r.get('data_type') or '').lower() != 'gtfs':
            continue
        if not ('xpress m' in prov.lower() or 'REM' in prov.split()):
            continue
        if (r.get('status') or '') in ('deprecated', 'inactive'):
            continue
        for k in ('urls.latest', 'urls.direct_download', 'urls.authentication_info'):
            u = r.get(k)
            if u and u.startswith('http') and k != 'urls.authentication_info':
                urls.append(u)
        print('catalog:', prov.strip(), r.get('location.municipality'), urls[-2:])
    if not urls:
        raise SystemExit('REM schedule not found in the catalog; set REM_GTFS_URL')
    return urls


def rows(z, name):
    if name not in z.namelist():
        return []
    with z.open(name) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig')))


def secs(t):
    h, m, s = (int(x) for x in t.strip().split(':'))
    return h * 3600 + m * 60 + s


def main(src=None):
    if src:
        data = open(src, 'rb').read()
    else:
        data, err = None, None
        for u in find_url():
            try:
                data = get(u)
                zipfile.ZipFile(io.BytesIO(data))
                print('schedule from', u.split('?')[0], len(data) // 1024, 'KB')
                break
            except Exception as e:
                err, data = e, None
        if data is None:
            raise SystemExit('could not download the REM schedule: %s' % err)
    z = zipfile.ZipFile(io.BytesIO(data))
    routes = rows(z, 'routes.txt')
    # (the REM's own file has only the REM; a regional file would also have buses: keep the REM's light rail route)
    rem_routes = {r['route_id'] for r in routes if r.get('route_type') in ('0', '1', '2', '12')
                  and ('REM' in (r.get('route_short_name', '') + r.get('route_long_name', '') + r.get('agency_id', ''))
                       or len(routes) <= 5)}
    if not rem_routes:
        rem_routes = {r['route_id'] for r in routes}
    trips = [t for t in rows(z, 'trips.txt') if t['route_id'] in rem_routes]
    print('routes', len(rem_routes), 'trips', len(trips))

    # shapes, with distances along them (metres)
    shp = collections.defaultdict(list)
    for r in rows(z, 'shapes.txt'):
        shp[r['shape_id']].append((int(r['shape_pt_sequence']), float(r['shape_pt_lon']), float(r['shape_pt_lat'])))
    used = {t.get('shape_id') for t in trips}
    shapes, cum = {}, {}
    for sid, pts in shp.items():
        if sid not in used:
            continue
        pts.sort()
        P = [(p[1], p[2]) for p in pts]
        c = [0.0]
        for i in range(1, len(P)):
            kx = 111320 * math.cos(math.radians(P[i][1]))
            c.append(c[-1] + math.hypot((P[i][0] - P[i - 1][0]) * kx, (P[i][1] - P[i - 1][1]) * 110540))
        flat = []
        for lon, lat in P:
            flat += [round(lon, 6), round(lat, 6)]
        shapes[sid] = flat
        cum[sid] = (P, c)

    stops = {s['stop_id']: s for s in rows(z, 'stops.txt')}

    def along(sid, lon, lat):
        """distance along the shape of the point nearest to a stop"""
        P, c = cum[sid]
        best, bd = 0.0, 1e18
        for i in range(len(P) - 1):
            kx = 111320 * math.cos(math.radians(lat))
            ax, ay = (P[i][0] - lon) * kx, (P[i][1] - lat) * 110540
            bx, by = (P[i + 1][0] - lon) * kx, (P[i + 1][1] - lat) * 110540
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy or 1e-9
            u = max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
            d = (ax + dx * u) ** 2 + (ay + dy * u) ** 2
            if d < bd:
                bd, best = d, c[i] + (c[i + 1] - c[i]) * u
        return best

    st = collections.defaultdict(list)
    for r in rows(z, 'stop_times.txt'):
        st[r['trip_id']].append(r)
    tripdata = {}
    for t in trips:
        sid = t.get('shape_id')
        if sid not in cum:
            continue
        S = sorted(st.get(t['trip_id'], []), key=lambda r: int(r['stop_sequence']))
        out = []
        for r in S:
            if not r.get('arrival_time') or not r.get('departure_time'):
                continue
            s = stops.get(r['stop_id'])
            if not s:
                continue
            d = float(r['shape_dist_traveled']) if r.get('shape_dist_traveled') else None
            if d is None or d <= 0 and out:
                d = along(sid, float(s['stop_lon']), float(s['stop_lat']))
            out.append([round(d), secs(r['arrival_time']), secs(r['departure_time'])])
        if len(out) >= 2:
            tripdata[t['trip_id']] = [sid, out, t['service_id']]
    # shape_dist_traveled may be in km or in the file's own unit: rescale to metres when it doesn't fit the shape
    for v in tripdata.values():
        L = cum[v[0]][1][-1]
        mx = max(p[0] for p in v[1])
        if mx > 0 and (mx < L * 0.5 or mx > L * 1.5):
            k = L / mx
            for p in v[1]:
                p[0] = round(p[0] * k)

    # which services run on which day
    cal = {r['service_id']: r for r in rows(z, 'calendar.txt')}
    exc = collections.defaultdict(dict)
    for r in rows(z, 'calendar_dates.txt'):
        exc[r['date']][r['service_id']] = r['exception_type']
    wd = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5))).date()
    os.makedirs(os.path.join(OUT, 'day'), exist_ok=True)
    for f in os.listdir(os.path.join(OUT, 'day')):
        os.remove(os.path.join(OUT, 'day', f))
    for k in range(-1, DAYS):
        day = today + datetime.timedelta(days=k)
        ds = day.strftime('%Y%m%d')
        on = set()
        for sid, r in cal.items():
            if r['start_date'] <= ds <= r['end_date'] and r.get(wd[day.weekday()]) == '1':
                on.add(sid)
        for sid, e in exc.get(ds, {}).items():
            if e == '1':
                on.add(sid)
            elif e == '2':
                on.discard(sid)
        T = [[v[0], v[1]] for v in tripdata.values() if v[2] in on]
        with open(os.path.join(OUT, 'day', ds + '.json'), 'w') as f:
            json.dump({'t': T}, f, separators=(',', ':'))
        print(ds, len(T), 'trips')
    names = [[s.get('stop_name', ''), round(float(s['stop_lat']), 6), round(float(s['stop_lon']), 6)]
             for s in stops.values() if s.get('stop_lat') and s.get('location_type', '0') in ('', '0', '1')]
    with open(os.path.join(OUT, 'net.json'), 'w') as f:
        json.dump({'v': 1, 'built': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d'),
                   'shapes': shapes, 'stops': names}, f, separators=(',', ':'))
    print('shapes', len(shapes), 'trips', len(tripdata))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else None)
