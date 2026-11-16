#!/usr/bin/env python3
"""Svingen: DATEX-II-Situationsfeed (Statens vegvesen) -> kompaktes oeffentliches JSON.

Zugangsdaten kommen NUR aus den Umgebungsvariablen DATEX_USER / DATEX_PASS
(GitHub-Actions-Secrets). Das Ergebnis enthaelt keine Zugangsdaten.
Datenquelle: Statens vegvesen, Lizenz NLOD 2.0.
"""
import base64, gzip, json, math, os, sys, time, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FEED = os.environ.get("DATEX_URL", "https://datex-server-get-v3-1.atlas.vegvesen.no/datexapi/GetSituation/pullsnapshotdata")
OSLO = ZoneInfo("Europe/Oslo")
AHEAD_DAYS = 3          # geplante Sperrungen, die innerhalb dieser Tage beginnen
SIMPLIFY_M = 4.0        # Douglas-Peucker-Toleranz in Metern

# Kategorien: DATEX-Typ/-Subtyp -> (Code)
RCLM = {
    "roadClosed": "gesperrt",
    "intermittentShortTermClosures": "kurz",
    "closedPermanentlyForTheWinter": "winter",
    "weightRestrictionInOperation": "gewicht",
    "laneClosures": "spur",
    "narrowLanes": "eng",
}
OBSTRUCTION_TYPES = {"Accident", "GeneralObstruction", "InfrastructureDamageObstruction",
                     "EnvironmentalObstruction", "VehicleObstruction"}


def ln(e):
    return e.tag.rsplit("}", 1)[-1]


def fetch():
    user, pw = os.environ.get("DATEX_USER"), os.environ.get("DATEX_PASS")
    if not user or not pw:
        sys.exit("DATEX_USER/DATEX_PASS fehlen")
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(FEED, headers={"Authorization": "Basic " + auth, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except Exception as ex:  # keine Details mit Zugangsdaten ausgeben
            last = type(ex).__name__ + " " + str(getattr(ex, "code", ""))
            time.sleep(5 * (attempt + 1))
    sys.exit("Feed nicht erreichbar: " + str(last))


# ---- ETRS89 / UTM 33N (EPSG:25833) -> WGS84 (Kruger-Reihe) ----
_a, _f = 6378137.0, 1 / 298.257222101
_n = _f / (2 - _f)
_A = _a / (1 + _n) * (1 + _n**2 / 4 + _n**4 / 64)
_beta = [_n / 2 - 2 * _n**2 / 3 + 37 * _n**3 / 96,
         _n**2 / 48 + _n**3 / 15,
         17 * _n**3 / 480]
_delta = [2 * _n - 2 * _n**2 / 3 - 2 * _n**3,
          7 * _n**2 / 3 - 8 * _n**3 / 5,
          56 * _n**3 / 15]
_k0, _E0, _lon0 = 0.9996, 500000.0, math.radians(15.0)


def utm33_to_wgs(E, N):
    xi = N / (_k0 * _A)
    eta = (E - _E0) / (_k0 * _A)
    xi_p, eta_p = xi, eta
    for j, b in enumerate(_beta, start=1):
        xi_p -= b * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        eta_p -= b * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
    chi = math.asin(math.sin(xi_p) / math.cosh(eta_p))
    lat = chi + sum(d * math.sin(2 * j * chi) for j, d in enumerate(_delta, start=1))
    lon = _lon0 + math.atan2(math.sinh(eta_p), math.cos(xi_p))
    return math.degrees(lat), math.degrees(lon)


def dp(points, tol):
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        md, mi = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            if L2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > md:
                md, mi = d, i
        if md > tol:
            keep[mi] = True
            stack.append((a, mi))
            stack.append((mi, b))
    return [p for p, k in zip(points, keep) if k]


def parse_time(s):
    if not s:
        return None
    s = s.strip()
    try:
        # Bruchteile >6 Stellen kuerzen
        if "." in s:
            head, rest = s.split(".", 1)
            frac = ""
            i = 0
            while i < len(rest) and rest[i].isdigit():
                i += 1
            frac, tz = rest[:i][:6], rest[i:]
            s = head + "." + frac + tz
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def period_active(vp, now_local):
    """Gilt eine validPeriod jetzt? (Tageszeiten werden als lokale Oslo-Zeit gelesen.)"""
    kids = {ln(c): c for c in vp}
    st = parse_time(kids["startOfPeriod"].text) if "startOfPeriod" in kids else None
    en = parse_time(kids["endOfPeriod"].text) if "endOfPeriod" in kids else None
    if st and now_local < st.astimezone(OSLO):
        return False
    if en and now_local > en.astimezone(OSLO):
        return False
    rt = kids.get("recurringTimePeriodOfDay")
    days = [d.text for d in vp.iter() if ln(d) == "applicableDay"]
    if rt is None and not days:
        return True
    cur = now_local.hour * 60 + now_local.minute
    wd = DAYS[now_local.weekday()]
    pd = DAYS[(now_local.weekday() - 1) % 7]
    if rt is not None:
        def hm(x):
            t = [c.text for c in rt if ln(c) == x][0]
            return int(t[0:2]) * 60 + int(t[3:5])
        try:
            a, b = hm("startTimeOfPeriod"), hm("endTimeOfPeriod")
        except Exception:
            return True
        if a <= b:
            ok = a <= cur <= b and (not days or wd in days)
        else:  # ueber Mitternacht
            ok = (cur >= a and (not days or wd in days)) or (cur < b and (not days or pd in days))
        return ok
    return wd in days


def first(e, name):
    for c in e.iter():
        if ln(c) == name:
            return c
    return None


def ftext(e, name):
    c = first(e, name)
    return c.text if c is not None else None


def texts(e, name):
    return [c.text.strip() for c in e.iter() if ln(c) == name and c.text and c.text.strip()]


def vehicle_restriction(rec):
    out = {}
    for v in rec.iter():
        if ln(v) != "forVehiclesWithCharacteristicsOf":
            continue
        d = {ln(c): (c.text or "").strip() for c in v.iter()}
        op = d.get("comparisonOperator")
        for key, name in (("vehicleLength", "len"), ("vehicleWidth", "wid"), ("vehicleHeight", "hgt"), ("grossVehicleWeight", "wgt")):
            if key in d:
                try:
                    out[name] = {"op": op, "v": float(d[key])}
                except ValueError:
                    pass
    return out


def geometry(rec):
    lines, pts = [], []
    for g in rec.iter():
        t = ln(g)
        if t == "gmlLineString":
            vals = [float(x) for x in (first(g, "posList").text or "").split()]
            xy = list(zip(vals[0::2], vals[1::2]))
            if len(xy) >= 2:
                xy = dp(xy, SIMPLIFY_M)
                lines.append([[round(la, 5), round(lo, 5)] for la, lo in (utm33_to_wgs(x, y) for x, y in xy)])
        elif t == "coordinatesForDisplay":
            la, lo = first(g, "latitude"), first(g, "longitude")
            if la is not None and lo is not None:
                pts.append([round(float(la.text), 5), round(float(lo.text), 5)])
    if lines:
        return lines, []
    # Punkt(e) ohne Linie
    seen, uniq = set(), []
    for p in pts:
        if tuple(p) not in seen:
            seen.add(tuple(p))
            uniq.append(p)
    return [], uniq[:1]


def main():
    now = datetime.now(timezone.utc)
    now_local = now.astimezone(OSLO)
    raw = fetch()
    root = ET.fromstring(raw)
    pub = first(root, "publicationTime")
    records = [e for e in root.iter() if ln(e) == "situationRecord"]
    if len(records) < 200:
        sys.exit(f"Feed unplausibel klein ({len(records)} Eintraege) - nichts veroeffentlicht")

    items = {}
    for rec in records:
        ty = [v for k, v in rec.attrib.items() if k.endswith("type")][0].split(":")[-1]
        cat = None
        if ty == "RoadOrCarriagewayOrLaneManagement":
            sub = first(rec, "roadOrCarriagewayOrLaneManagementType")
            cat = RCLM.get(sub.text if sub is not None else "")
        elif ty in OBSTRUCTION_TYPES:
            cat = "hindernis"
        if not cat:
            continue
        ovs = parse_time(ftext(rec, "overallStartTime"))
        ove = parse_time(ftext(rec, "overallEndTime"))
        if ove and ove < now:
            continue
        if ovs and ovs > now + timedelta(days=AHEAD_DAYS):
            continue
        if ovs and ovs > now:
            phase = "geplant"
        else:
            periods = [p for p in rec.iter() if ln(p) == "validPeriod"]
            if periods and not any(period_active(p, now_local) for p in periods):
                phase = "zeitweise"
            else:
                phase = "jetzt"
        vr = vehicle_restriction(rec)
        lines, pts = geometry(rec)
        if not lines and not pts:
            continue
        comments = []
        for cm in rec.iter():
            if ln(cm) == "generalPublicComment":
                for v in cm.iter():
                    if ln(v) == "value" and v.text:
                        for part in v.text.replace("\n", " ").split("|"):
                            part = part.strip()
                            if part and part not in comments:
                                comments.append(part)
        road_nr = texts(rec, "roadNumber")
        road_nm = texts(rec, "roadName")
        # Beschreibung der Lokalisierung (locationDescription)
        loc = first(rec, "locationDescription")
        loc_txt = ""
        if loc is not None:
            v = first(loc, "value")
            loc_txt = (v.text or "").strip() if v is not None else ""
        area = ""
        an = first(rec, "areaName")
        if an is not None:
            v = first(an, "value")
            area = (v.text or "").strip() if v is not None else ""
        item = {
            "i": rec.attrib.get("id", ""),
            "c": cat,
            "p": phase,
            "a": ovs.astimezone(OSLO).strftime("%Y-%m-%dT%H:%M") if ovs else None,
            "e": ove.astimezone(OSLO).strftime("%Y-%m-%dT%H:%M") if ove else None,
            "r": road_nr[0] if road_nr else "",
            "n": road_nm[0] if road_nm else "",
            "l": loc_txt[:160],
            "o": area,
            "t": comments[:4],
            "f": vr,
            "g": lines,
            "q": pts,
        }
        # NOTE: "g" Linien als [[lat,lng],...]; "q" einzelne Punkte [[lat,lng]]
        items[item["i"]] = item

    out = sorted(items.values(), key=lambda x: (x["c"], x["r"], x["i"]))
    result = {
        "v": 1,
        "erzeugt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feed": pub.text if pub is not None else None,
        "quelle": "Statens vegvesen (DATEX II), NLOD 2.0",
        "anzahl": len(out),
        "s": out,
    }
    outdir = sys.argv[1] if len(sys.argv) > 1 else "public"
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "sperrungen.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, separators=(",", ":"))
    by = {}
    for x in out:
        by[(x["c"], x["p"])] = by.get((x["c"], x["p"]), 0) + 1
    print("Eintraege:", len(out), "Bytes:", os.path.getsize(path))
    for k in sorted(by):
        print(" ", k, by[k])


if __name__ == "__main__":
    main()
