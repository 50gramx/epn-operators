#!/usr/bin/env python3
r"""Bake one region's 3D scene, inside the pod, and hand it back as a tar.

WHAT THIS IS
------------
The real bake. This file used to say the pipeline "lives outside this repo
today, in C:\gtpoc\build_scene.py, and has to be carried in here before a
region gets a scene anyone would want to look at". This is that carry.

The geometry below is the SAME CODE, not a second version of it: ear clipping,
the Douglas-Peucker simplifier, the extruder, the road ribbons and the tile
grid are verbatim. A region baked automatically by a gram and a region baked by
hand have to be the same region, and two implementations of a triangulator
diverge quietly -- the buildings just get subtly wrong somewhere.

WHAT CHANGED, AND ONLY THIS
---------------------------
The original packs its buffers with numpy. The runner image is a bare python
and every authored program shares it, so adding a wheel there changes the image
for all of them. array('f') writes byte-identical little-endian float32, so the
packing goes through that instead. Nothing else was touched.

WHAT IS NOT CARRIED
-------------------
The splat path. It is the one part that genuinely needs numpy -- a
quaternion-to-covariance per splat -- and a region that has just joined the
network has no capture to place anyway. So this emits no splat key rather than
an empty one, and a region WITH a capture is still baked by the pipeline
outside until that path is ported. Saying so beats a scene that looks captured
and is not.

WHERE THE DATA COMES FROM
-------------------------
Nominatim for the pincode's bounds, Overpass for what is inside them. Both are
public OSM infrastructure with usage policies, so both are called once per
bake, with a real User-Agent and a bounded area. A bake with no route out fails
loudly instead of writing an empty region -- an empty scene that claims to be
complete is a region that looks baked and is not, which nobody notices until
someone opens it.

WHY IT IS A SERVER AND NOT A SCRIPT
-----------------------------------
The runner image starts one process and the daemon reaches it through the API
server's service proxy -- the same path every other authored program is called
on. A script that exits would need a Job, a completion watch and a volume to
leave its output on; a server hands the bytes back on the response and leaves
nothing behind to garbage-collect.

Stdlib only: the runner image is a bare python, and a bake that needs a wheel
downloaded at pod start is a bake that fails the first time the region has no
route out.
"""

import base64
import io
import json
import math
import os
import re
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from array import array
from http.server import BaseHTTPRequestHandler, HTTPServer

# A pincode arrives from the daemon, which read it off a signed location
# bench -- but it becomes a path segment inside the tar, so it is checked
# here too. A component validated only by its sender is a component
# validated only until someone else calls this.
PINCODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,15}$")

# The tar crosses back as base64 inside a JSON body, through kubectl's stdout.
# That path is fine for an index and some buffers and is NOT fine for an
# unbounded blob, so the bake refuses to produce one rather than discovering
# the limit as a truncated response the daemon cannot parse.
MAX_TAR_BYTES = 64 * 1024 * 1024

# OSM asks anyone automating against its infrastructure to identify themselves
# and not hammer it. This is that identification; the caps below are the rest
# of the bargain.
USER_AGENT = "epn-daemon region-scene-bake (+https://50gramx.com)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# The reverse of the above: a point on the earth to the postcode that contains
# it. This is what a person WALKING toward the edge of a region needs (RW-5):
# they have coordinates, not a pincode, and a browser must never call a public
# geocoder itself -- the courtesy gap, the USER_AGENT and the rate budget all
# live here, on one machine, where they can be honoured.
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
HTTP_TIMEOUT = 180

# Half-extent, metres, LARGEST FIRST -- the bake walks down this list until
# Overpass answers.
#
# A single constant cannot be right here. A postcode's own bounds can be tens
# of kilometres, and 6 km each way over a sparse tech park is Electronic City
# while the same box over a dense city centre is half a million buildings:
# Overpass closes the connection, and if it did not, the tar would be over the
# 64 MB the response can carry. Guessing smaller to be safe makes every sparse
# region a postage stamp.
#
# So the area is discovered instead of assumed. Shrinking is honest -- the
# scene records the bbox it actually used -- and a region that is too dense for
# 5 km simply gets a tighter, complete scene rather than no scene at all.
HALF_M_LADDER = (2500.0, 1500.0, 900.0, 500.0)
MAX_BUILDINGS = 40000

TILE_M = 500.0
FLOOR_M = 3.2
COARSE_H = 15.0
COARSE_A = 2000.0
DP_TOL = 2.0
COARSE_ROAD_RANK = 3

# fallback storeys when OSM says nothing, by what the building claims to be
BY_KIND = {
    "apartments": 4, "residential": 3, "commercial": 3, "office": 4,
    "retail": 2, "industrial": 2, "warehouse": 2, "school": 2,
    "college": 3, "university": 3, "hospital": 4, "hotel": 4,
    "house": 1, "detached": 1, "bungalow": 1, "hut": 1, "shed": 1,
    "garage": 1, "garages": 1, "roof": 1, "construction": 3,
}

ROAD_RANK = {
    "motorway": 5, "trunk": 5, "primary": 4, "secondary": 3,
    "tertiary": 2, "residential": 1, "unclassified": 1,
    "living_street": 1, "service": 0,
}

ROAD_WIDTH = {5: 11.0, 4: 8.0, 3: 6.5, 2: 5.0, 1: 3.5, 0: 2.5}


def num(v):
    if v is None:
        return None
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)", str(v))
    return float(m.group(1)) if m else None


def ring_area_m2(ring, lat0):
    """Shoelace in local metres. Good enough to size a building."""
    if len(ring) < 3:
        return 0.0
    mx = 111320.0 * math.cos(math.radians(lat0))
    my = 110540.0
    s = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0] * mx, ring[i][1] * my
        x2, y2 = ring[i + 1][0] * mx, ring[i + 1][1] * my
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def height_for(tags, area, kind):
    """A height, AND where it came from.

    Only a few percent of Indian OSM buildings carry one, so most are
    estimated -- and an estimated skyline should admit it rather than passing
    as survey data. The viewer colours by exactly this.
    """
    h = num(tags.get("height"))
    if h and 1.5 < h < 400:
        return round(h, 1), "tag"
    lv = num(tags.get("building:levels"))
    if lv and 0 < lv < 120:
        return round(lv * FLOOR_M, 1), "levels"
    if kind in BY_KIND:
        return round(BY_KIND[kind] * FLOOR_M, 1), "est_kind"
    if area < 80:
        f = 1
    elif area < 300:
        f = 2
    elif area < 1200:
        f = 3
    else:
        f = 4
    return round(f * FLOOR_M, 1), "est_area"


def thin(ring, tol_deg):
    """Drop points closer than tol to the previous kept one; ring stays closed."""
    if len(ring) < 5:
        return ring
    out = [ring[0]]
    for p in ring[1:-1]:
        q = out[-1]
        if abs(p[0] - q[0]) > tol_deg or abs(p[1] - q[1]) > tol_deg:
            out.append(p)
    out.append(ring[-1] if ring[-1] != ring[0] else ring[0])
    return out if len(out) >= 4 else ring


def signed_area(p):
    s = 0.0
    for i in range(len(p)):
        x1, y1 = p[i]
        x2, y2 = p[(i + 1) % len(p)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def point_in_tri(p, a, b, c):
    d1 = (p[0]-b[0])*(a[1]-b[1]) - (a[0]-b[0])*(p[1]-b[1])
    d2 = (p[0]-c[0])*(b[1]-c[1]) - (b[0]-c[0])*(p[1]-c[1])
    d3 = (p[0]-a[0])*(c[1]-a[1]) - (c[0]-a[0])*(p[1]-a[1])
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)


def earclip(poly):
    """Triangulate a simple polygon. Returns index triples into poly."""
    n = len(poly)
    if n < 3:
        return []
    idx = list(range(n))
    if signed_area(poly) < 0:
        idx.reverse()
    out = []
    guard = 0
    while len(idx) > 3 and guard < 4 * n:
        guard += 1
        clipped = False
        for k in range(len(idx)):
            i0, i1, i2 = idx[k-1], idx[k], idx[(k+1) % len(idx)]
            a, b, c = poly[i0], poly[i1], poly[i2]
            if (b[0]-a[0])*(c[1]-a[1]) - (c[0]-a[0])*(b[1]-a[1]) <= 0:
                continue                      # reflex
            if any(point_in_tri(poly[j], a, b, c)
                   for j in idx if j not in (i0, i1, i2)):
                continue
            out.append((i0, i1, i2))
            idx.pop(k)
            clipped = True
            break
        if not clipped:
            break
    if len(idx) == 3:
        out.append(tuple(idx))
    return out


def _dp(pts, tol):
    """Douglas-Peucker on an open polyline."""
    if len(pts) < 3:
        return list(pts)
    ax, ay = pts[0]
    bx, by = pts[-1]
    dx, dy = bx - ax, by - ay
    L2 = dx*dx + dy*dy
    worst, wi = -1.0, 0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        if L2 < 1e-12:
            d = math.hypot(px - ax, py - ay)
        else:
            t = max(0.0, min(1.0, ((px-ax)*dx + (py-ay)*dy) / L2))
            d = math.hypot(px - (ax + t*dx), py - (ay + t*dy))
        if d > worst:
            worst, wi = d, i
    if worst <= tol:
        return [pts[0], pts[-1]]
    left = _dp(pts[:wi+1], tol)
    right = _dp(pts[wi:], tol)
    return left[:-1] + right


def simplify_ring(ring, tol):
    """Simplify a closed ring, keeping it closed and non-degenerate.

    Cut at the vertex furthest from vertex 0 so the result is not biased by
    wherever OSM happened to start the way.
    """
    n = len(ring)
    if n < 5:
        return ring
    x0, y0 = ring[0]
    far = max(range(n), key=lambda i: (ring[i][0]-x0)**2 + (ring[i][1]-y0)**2)
    a = _dp(ring[0:far+1], tol)
    b = _dp(ring[far:] + [ring[0]], tol)
    out = a[:-1] + b[:-1]
    if len(out) < 3 or abs(signed_area(out)) < 1.0:
        return ring
    return out


def building_tris(ring, h, meas, sink):
    """Walls + roof for one footprint, appended to sink as x,y,z,kind,meas."""
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        for (px, py, pz) in ((x1, y1, 0), (x2, y2, 0), (x2, y2, h),
                             (x1, y1, 0), (x2, y2, h), (x1, y1, h)):
            sink.extend((px, py, pz, 0.0, meas))
    tris = earclip(ring)
    for (i0, i1, i2) in tris:
        for i in (i0, i1, i2):
            sink.extend((ring[i][0], ring[i][1], h, 1.0, meas))
    return bool(tris)


def cell_of(x, y):
    return (int(math.floor(x / TILE_M)), int(math.floor(y / TILE_M)))


class Tile(object):
    __slots__ = ("ix", "iy", "b0", "b1", "r0", "r1",
                 "zmin", "zmax", "xmin", "xmax", "ymin", "ymax")

    def __init__(self, ix, iy):
        self.ix, self.iy = ix, iy
        self.b0, self.b1, self.r0, self.r1 = [], [], [], []
        self.zmin, self.zmax = 0.0, 0.0
        self.xmin = ix * TILE_M
        self.xmax = (ix + 1) * TILE_M
        self.ymin = iy * TILE_M
        self.ymax = (iy + 1) * TILE_M

    def grow(self, x, y, z):
        # geometry belongs to the tile of its anchor, so it can spill a little
        # past the cell edge; the index records where it really is
        if x < self.xmin: self.xmin = x
        if x > self.xmax: self.xmax = x
        if y < self.ymin: self.ymin = y
        if y > self.ymax: self.ymax = y
        if z < self.zmin: self.zmin = z
        if z > self.zmax: self.zmax = z


def _get(url, data=None, accept_json=True):
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", USER_AGENT)
    if accept_json:
        req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def postcode_at(lat, lon):
    """The postcode containing a point, or "" when none does.
    
    zoom=18 asks for building-level detail, which is what carries a postcode
    in the address parts; a coarser zoom answers with a district and no
    postcode at all. Returns "" rather than guessing: a neighbour named wrong
    is a region baked for nobody.
    """
    q = urllib.parse.urlencode({
        "lat": "%.6f" % lat, "lon": "%.6f" % lon,
        "format": "json", "zoom": "18", "addressdetails": "1",
    })
    hit = _get(NOMINATIM_REVERSE_URL + "?" + q)
    if not isinstance(hit, dict):
        return ""
    pin = str((hit.get("address") or {}).get("postcode", "")).strip()
    return pin if PINCODE.match(pin) else ""


def geocode(pincode):
    """Pincode to centre and bounds, from Nominatim.

    Asked as a postalcode rather than as free text, so "560100" cannot match a
    house number or a road with that name somewhere else in the world.
    """
    q = urllib.parse.urlencode({
        "postalcode": pincode, "country": "India",
        "format": "json", "limit": "1", "addressdetails": "1",
    })
    hits = _get(NOMINATIM_URL + "?" + q)
    if not hits:
        raise RuntimeError("no place found for pincode %s" % pincode)
    h = hits[0]
    lat, lon = float(h["lat"]), float(h["lon"])
    # Nominatim gives [south, north, west, east] as strings.
    bb = [float(v) for v in h.get("boundingbox", [])]

    # A NAME A PERSON WOULD USE, not the number they already typed.
    #
    # display_name for a postcode search starts with the postcode itself, so
    # splitting on the first comma just hands back "500050". The address parts
    # carry the actual place, smallest first -- a suburb if OSM knows one, then
    # the town, then the city. Falling back to the pincode is honest when
    # nothing better exists, and better than inventing one.
    a = h.get("address") or {}
    local = ""
    for key in ("suburb", "neighbourhood", "quarter", "village", "town",
                "city_district", "municipality", "county", "state_district"):
        if a.get(key):
            local = a[key]
            break
    city = a.get("city") or a.get("state_district") or a.get("state") or ""
    if local and city and city.lower() not in local.lower():
        label = "%s, %s" % (local, city)      # "Chandanagar, Hyderabad"
    else:
        label = local or city
    return lat, lon, (bb if len(bb) == 4 else None), (label or pincode)


def bounds_for(lat, lon, bb, half_m):
    """A bbox in degrees, centred on the place and capped.

    A postcode's own bounds can be enormous -- a rural pincode covers tens of
    kilometres -- and the whole of it would blow the Overpass quota and the
    64 MB the response can carry. So the cap is the real rule and Nominatim's
    bounds only ever shrink it.
    """
    mE = 111320.0 * math.cos(math.radians(lat))
    mN = 110540.0
    dlon = half_m / mE
    dlat = half_m / mN
    if bb:
        dlat = min(dlat, max((bb[1] - bb[0]) / 2.0, 1e-4))
        dlon = min(dlon, max((bb[3] - bb[2]) / 2.0, 1e-4))
    return [lon - dlon, lat - dlat, lon + dlon, lat + dlat], mE, mN


def overpass(bbox):
    """Buildings and roads inside the bbox, with geometry.

    'out geom' rather than node ids, because resolving ways to nodes here would
    be a second pass over a much larger response for the same answer.
    """
    s = "%f,%f,%f,%f" % (bbox[1], bbox[0], bbox[3], bbox[2])   # S,W,N,E
    query = (
        "[out:json][timeout:%d];(" % (HTTP_TIMEOUT - 20) +
        'way["building"](' + s + ');' +
        'way["highway"](' + s + ');' +
        ");out geom;"
    )
    body = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for url in OVERPASS_URLS:
        try:
            return _get(url, data=body)
        except Exception as exc:                       # noqa: BLE001
            # A mirror being busy is ordinary; both being unreachable is not,
            # and that is the case worth reporting rather than papering over.
            last = exc
            time.sleep(2)
    raise RuntimeError("overpass unreachable: %s" % last)


def region_from_osm(osm, lat0, lon0, label, pincode):
    """Buildings with heights and provenance, and ranked roads."""
    buildings, roads = [], []
    prov = {}
    for e in osm.get("elements", []):
        t = e.get("tags") or {}
        g = e.get("geometry")
        if not g or len(g) < 2:
            continue
        ring = [[round(p["lon"], 6), round(p["lat"], 6)] for p in g]
        if "building" in t:
            kind = t.get("building", "yes")
            area = ring_area_m2(ring, lat0)
            if area < 12:            # sheds, map noise
                continue
            h, src = height_for(t, area, kind)
            prov[src] = prov.get(src, 0) + 1
            buildings.append({"g": thin(ring, 2e-6), "h": h, "s": src})
        elif "highway" in t:
            roads.append({"g": ring, "r": ROAD_RANK.get(t.get("highway"), 0)})
    # Biggest first, so a cap keeps the buildings that carry the skyline.
    buildings.sort(key=lambda b: -b["h"])
    if len(buildings) > MAX_BUILDINGS:
        buildings = buildings[:MAX_BUILDINGS]
    return {"buildings": buildings, "roads": roads,
            "provenance": prov, "label": label, "pincode": pincode}


def bake(pincode):
    """Produce the scene's files as {name: bytes}.

    The frame is the contract the viewer and every later capture agree on: one
    local ENU origin, metres, +x east, +y north, +z up, and a tile grid
    anchored on that origin rather than on the bbox -- so re-baking with more
    data does not renumber the tiles under a viewer already holding some.
    """
    lat0, lon0, bb, label = geocode(pincode)
    osm = None
    for half in HALF_M_LADDER:
        bbox, mE, mN = bounds_for(lat0, lon0, bb, half)
        try:
            osm = overpass(bbox)
            break
        except Exception as exc:                       # noqa: BLE001
            # Too much ground for the API to answer, almost always. Say which
            # rung failed: a bake that quietly returns a 500 m scene for a
            # whole city should be visible in the log, not a mystery later.
            print("region-scene: %s at %.0f m failed (%s), trying tighter"
                  % (pincode, half, exc), flush=True)
    if osm is None:
        raise RuntimeError("overpass would not answer for %s at any size" % pincode)
    region = region_from_osm(osm, lat0, lon0, label, pincode)

    def enu(lon, lat):
        return ((lon - lon0) * mE, (lat - lat0) * mN)

    tiles = {}

    def tile(ix, iy):
        t = tiles.get((ix, iy))
        if t is None:
            t = tiles[(ix, iy)] = Tile(ix, iy)
        return t

    ntri_full = ntri_coarse = 0
    ncoarse = nfail = 0
    for b in region["buildings"]:
        ring = [enu(x, y) for x, y in b["g"]]
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) < 3:
            continue
        h = float(b["h"])
        meas = 1.0 if b["s"] in ("tag", "levels") else 0.0
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        t = tile(*cell_of(cx, cy))

        before = len(t.b1)
        if not building_tris(ring, h, meas, t.b1):
            nfail += 1
        ntri_full += (len(t.b1) - before) // 5 // 3
        for (px, py) in ring:
            t.grow(px, py, h)

        # L0 keeps only what still reads as a shape from ~900 m out.
        area = abs(signed_area(ring))
        if h >= COARSE_H or area >= COARSE_A:
            simple = simplify_ring(ring, DP_TOL)
            before = len(t.b0)
            building_tris(simple, h, meas, t.b0)
            ntri_coarse += (len(t.b0) - before) // 5 // 3
            ncoarse += 1

    nroad_full = nroad_coarse = 0
    for r in region["roads"]:
        pts = [enu(x, y) for x, y in r["g"]]
        rank = float(r.get("r", 0))
        w = ROAD_WIDTH.get(r.get("r", 0), 3.0) * 0.5
        coarse = r.get("r", 0) >= COARSE_ROAD_RANK
        for i in range(len(pts) - 1):
            (x1, y1), (x2, y2) = pts[i], pts[i+1]
            dx, dy = x2-x1, y2-y1
            L = math.hypot(dx, dy)
            if L < 1e-6:
                continue
            nx, ny = -dy/L*w, dx/L*w
            # a road crossing a boundary simply continues in the next tile
            t = tile(*cell_of((x1+x2)*0.5, (y1+y2)*0.5))
            quad = ((x1+nx, y1+ny), (x2+nx, y2+ny), (x2-nx, y2-ny),
                    (x1+nx, y1+ny), (x2-nx, y2-ny), (x1-nx, y1-ny))
            for (px, py) in quad:
                t.r1.extend((px, py, 0.05, rank))
                t.grow(px, py, 0.05)
            nroad_full += 2
            if coarse:
                for (px, py) in quad:
                    t.r0.extend((px, py, 0.05, rank))
                nroad_coarse += 2

    files = {}
    index = []
    totals = {"b0": 0, "b1": 0, "r0": 0, "r1": 0}
    for (ix, iy), t in sorted(tiles.items()):
        tid = "%d_%d" % (ix, iy)
        parts = {}
        for key, floats, stride in (("b0", t.b0, 5), ("b1", t.b1, 5),
                                    ("r0", t.r0, 4), ("r1", t.r1, 4)):
            if not floats:
                continue
            # array('f') is byte-identical to numpy float32 here, which is the
            # whole reason the carried pipeline needed no other change.
            blob = array("f", floats).tobytes()
            fn = "tiles/%s.%s.bin" % (tid, key)
            files["scene/" + fn] = blob
            parts[key] = {"file": fn, "bytes": len(blob),
                          "tris": len(floats) // stride // 3}
            totals[key] += len(blob)
        if not parts:
            continue
        index.append({
            "id": tid,
            "cell": [ix, iy],
            "bbox": [round(t.xmin, 2), round(t.ymin, 2), round(t.zmin, 2),
                     round(t.xmax, 2), round(t.ymax, 2), round(t.zmax, 2)],
            "lods": sorted({0 for k in parts if k.endswith("0")} |
                           {1 for k in parts if k.endswith("1")}),
            "parts": parts,
        })

    prov = region["provenance"]
    counts = {
        "buildings": len(region["buildings"]),
        "triangles": ntri_full,
        "coarse_triangles": ntri_coarse,
        "road_triangles": nroad_full,
        "tiles": len(index),
    }

    meta = {
        "pincode": pincode,
        "label": region["label"],
        "centre": [round(lon0, 6), round(lat0, 6)],
        "bbox": [round(v, 7) for v in bbox],
        "metres_per_deg": [round(mE, 6), round(mN, 1)],
        "counts": counts,
        "height_provenance": prov,
        "attribution": "Map data (c) OpenStreetMap contributors, ODbL",
        "mesh_stride": 5,
        "roads_stride": 4,
        "tiled": True,
        "tile_m": TILE_M,
    }

    tiles_json = {
        "version": 1,
        "tile_m": TILE_M,
        "grid_origin": [0.0, 0.0],   # cells are anchored on the ENU origin
        "mesh_stride": 5,            # x,y,z,kind,measured
        "roads_stride": 4,           # x,y,z,rank
        "splat_layout": "pos f32x3|scale f32x3|rot f32x4|rgb u8x3|op u8",
        "lods": [0, 1],
        "counts": counts,
        "bytes": totals,
        "tiles": index,
    }

    manifest = {
        "pincode": pincode,
        "frame": {"kind": "enu", "units": "metres", "axes": "x=east,y=north,z=up"},
        "tile_metres": TILE_M,
        # NO BAKE TIME IN THE CONTENT. The build doc states the invariant:
        # "two grams baking the same region produce the same bytes" -- which is
        # what makes a region ONE world rather than one per gram. A wall-clock
        # second in the manifest broke it on every bake: four grams held four
        # different CIDs for Hyderabad, none of them wrong, none of them the
        # same, and replication could never converge because there was no
        # shared object to converge on.
        #
        # WHEN a scene was baked is not part of WHAT it is. It already lives
        # outside the bytes, on the artifact statement's revision, which is
        # what orders two statements about one region. Nothing read this field.
        # Complete means there is geometry to look at. It does NOT claim a
        # capture: no splat has been placed here, and a region with one is
        # still baked by the pipeline outside until that path is carried in.
        "complete": bool(index),
        "counts": counts,
        "source": "OpenStreetMap via Overpass; bounds from Nominatim",
    }

    files["scene/scene.json"] = json.dumps(meta, indent=2, sort_keys=True).encode()
    files["scene/tiles.json"] = json.dumps(tiles_json, indent=2, sort_keys=True).encode()
    files["scene.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode()
    return files


# MANIFEST_NAME is where "complete" actually lives, and getting this wrong
# refused every bake on the network.
#
# The manifest is written as scene.json -- see to_tar's caller above. A checker
# that looked for "meta.json" matched nothing, fell through to its default, and
# reported every scene incomplete: 1.8 MB and 304 real tiles for 769002, baked
# in 3.7 seconds, declined before a byte moved because the file it asked for
# does not exist. Named as a constant so the writer and the reader cannot drift
# again.
MANIFEST_NAME = "scene.json"


def scene_is_complete(tar_bytes):
    """Read the producer's own verdict back out of the tar it just wrote.

    Returns True, False, or None for "could not tell".

    THE THIRD VALUE IS LOAD-BEARING and its absence is what broke this. An
    unreadable tar is not an incomplete scene, and collapsing the two into one
    boolean turns every question this function cannot answer into a refusal.
    The rule the rest of this codebase already follows, in those words: absent
    means UNKNOWN, never incomplete.

    Read rather than remembered so the answer cannot drift from what is
    actually in the bytes: an empty scene that claims to be complete is a
    region that looks baked and is not, which nobody notices until someone
    opens it.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
            for member in tf.getmembers():
                # Exact, not endswith: "scene/scene.json" is the per-tile meta
                # and carries no "complete" at all, so a suffix match would find
                # the wrong file and read a missing key as False.
                if member.name != MANIFEST_NAME:
                    continue
                fh = tf.extractfile(member)
                if fh is None:
                    return None
                manifest = json.loads(fh.read())
                if "complete" not in manifest:
                    return None  # a producer too old to say
                return bool(manifest["complete"])
    except Exception:
        return None
    return None


def to_tar(files):
    """Pack the files deterministically.

    Sorted names, zeroed mtime/uid/gid: two grams that bake the same region
    from the same data must produce the same bytes, or they content-address to
    two CIDs, announce them both, and share nothing. The daemon re-normalises
    on the way into the store as well; agreeing here costs nothing and makes
    the pod's own output comparable.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in sorted(files):
            data = files[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# -- WHY THE SCENE NO LONGER RIDES ON THE BAKE RESPONSE ----------------------
#
# The daemon reaches this program through the API server's service proxy, and a
# POST goes through `kubectl create --raw`, which reads the body from stdin and
# writes the response to stdout. Every bake on the fleet has failed there with
# "the server rejected our request for an unknown reason" -- which kubectl
# prints for BOTH an API-server rejection AND a pod returning 400 through the
# proxy, so the transport erases the difference and every theory about it has
# been unfalsifiable.
#
# The one thing that path does which no other call on it does is carry up to 85
# MB of base64 back on stdout. The older backlog predicted exactly this before
# it happened -- "the part to distrust is kubectl create --raw carrying a
# multi-MB base64 body on stdout".
#
# So the bake answers with a few hundred bytes, and the bytes are collected
# afterwards in bounded pieces over `kubectl get --raw`, which is the call
# podmetrics.py has used against the kubelet all along and which is designed to
# stream a response. No single message is large, so nothing depends on any
# limit anybody has to guess at.
#
# HELD IN MEMORY, ONE AT A TIME, AND DELIBERATELY. A scene is at most 64 MiB
# against this pod's 2 GiB limit, the daemon collects it immediately, and the
# pod is scaled back to zero when the bake is released. A disk would mean a
# volume, a cleanup path and a way to leak; a dict that holds the last bake and
# forgets the one before it cannot.
BAKES = {}

# CHUNK_BYTES bounds one collection response, base64 included. 3 MiB of tar is
# 4 MiB encoded, matching the object store's own ChunkSize -- one number for
# "how much of a large thing moves at once" rather than two that drift.
CHUNK_BYTES = 3 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        """Read the request body, chunked or not.

        THIS IS R-4, AND IT WAS OURS.

        The daemon reaches this program through the API server's service proxy,
        which means the request is made by `kubectl create --raw ... -f -`.
        kubectl reads stdin and sends it with **Transfer-Encoding: chunked and
        no Content-Length** -- measured, not assumed:

            method=POST  len_hdr=""  ContentLength=-1  TE=[chunked]  body=21

        BaseHTTPRequestHandler does not decode chunked request bodies. It is an
        HTTP/1.0-era server and leaves that to the handler. So the old code read
        Content-Length, got nothing, defaulted to 0, read zero bytes, parsed
        "{}", found no pincode, and answered:

            400 {"error": "refusing to bake a region I cannot name: ''"}

        kubectl then received a 400 whose body is not a Kubernetes Status and
        printed "the server rejected our request for an unknown reason" -- the
        sentence that has stood as R-4 since the first bake, blamed on the
        API server, on a multi-MB base64 body, and on the proxy path, and caused
        by none of them. 48 bake attempts across two machines and two operating
        systems, every one refused by this program for being asked nothing.

        Chunked is the case that matters and Content-Length is kept because a
        plain HTTP caller (the local test harness, curl, a NodePort) sends it.
        """
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            out = []
            while True:
                line = self.rfile.readline()
                if not line:
                    break  # the peer went away mid-body
                # A chunk header may carry extensions after a semicolon.
                size_part = line.strip().split(b";")[0]
                if not size_part:
                    continue
                try:
                    size = int(size_part, 16)
                except ValueError:
                    break  # not a chunk header; refuse rather than guess
                if size == 0:
                    # Trailers, then the final blank line.
                    while True:
                        trailer = self.rfile.readline()
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            break
                    break
                out.append(self.rfile.read(size))
                self.rfile.read(2)  # the CRLF that ends every chunk
            return b"".join(out)

        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def do_GET(self):
        # Readiness. The daemon waits on the Deployment, but a probe that asks
        # the program itself is what distinguishes "pod scheduled" from
        # "program running".
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path in ("", "/healthz"):
            self._json(200, {"ok": True})
            return
        if path == "/scene":
            q = urllib.parse.parse_qs(parsed.query)
            bake_id = (q.get("id") or [""])[0]
            tar = BAKES.get(bake_id)
            if tar is None:
                # A bake the pod no longer holds. Says so rather than
                # answering an empty body, which the daemon would store as a
                # scene and name for ever.
                self._json(404, {"error": "no bake here under that id -- the pod was restarted or released"})
                return
            try:
                offset = int((q.get("offset") or ["0"])[0])
            except ValueError:
                self._json(400, {"error": "offset must be a number"})
                return
            if offset < 0 or offset > len(tar):
                self._json(400, {"error": "offset %d is outside a %d byte scene" % (offset, len(tar))})
                return
            piece = tar[offset:offset + CHUNK_BYTES]
            self._json(200, {
                "offset": offset,
                "length": len(piece),
                "total": len(tar),
                "b64": base64.b64encode(piece).decode(),
            })
            return
        if path == "/where":
            # WHICH REGION IS THIS POINT IN (RW-5). Answers a pincode for a
            # lat/lon so the gram can turn "somebody walked toward here" into
            # a want. It does NOT bake: naming a region and producing one are
            # different costs and only one of them should happen because a
            # camera moved.
            q = urllib.parse.parse_qs(parsed.query)
            try:
                lat = float((q.get("lat") or [""])[0])
                lon = float((q.get("lon") or [""])[0])
            except ValueError:
                self._json(400, {"error": "lat and lon must be numbers"})
                return
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                self._json(400, {"error": "that point is not on the earth"})
                return
            try:
                pin = postcode_at(lat, lon)
            except Exception as exc:
                self._json(502, {"error": "could not name that place: %s" % exc})
                return
            if not pin:
                # Honest emptiness. Plenty of the earth has no postcode, and
                # answering one that is merely NEAR would send a walker into
                # the wrong region.
                self._json(404, {"error": "no postcode covers that point"})
                return
            self._json(200, {"pincode": pin})
            return
        self._json(404, {"error": "no such path"})

    def do_POST(self):
        if self.path.rstrip("/") != "/bake":
            self._json(404, {"error": "no such path"})
            return
        try:
            req = json.loads(self._read_body() or b"{}")
            pincode = str(req.get("pincode", "")).strip()
        except Exception as exc:
            self._json(400, {"error": "unreadable request: %s" % exc})
            return

        if not PINCODE.match(pincode):
            self._json(400, {"error": "refusing to bake a region I cannot name: %r" % pincode})
            return

        try:
            tar = to_tar(bake(pincode))
        except Exception as exc:
            self._json(500, {"error": "bake failed: %s" % exc})
            return

        if len(tar) > MAX_TAR_BYTES:
            self._json(500, {
                "error": "scene is %d bytes, over the %d a bake may produce" % (len(tar), MAX_TAR_BYTES),
            })
            return

        # ONE AT A TIME. The previous bake's bytes are dropped rather than
        # accumulated: the daemon collects immediately, and a pod that kept
        # every scene it had ever produced would be a memory limit waiting to
        # be hit on the busiest gram.
        BAKES.clear()
        BAKES[pincode] = tar

        # WHAT THE PRODUCER ITSELF SAYS, said OUT HERE where the daemon can act
        # on it before spending a single byte of transfer. The same value is
        # inside the tar's meta -- see the "complete" field bake() writes -- and
        # for a fortnight nothing on the Go side read it, so an empty scene was
        # named, signed and gossiped like any other. Stating it in the response
        # makes refusing one cost nothing.
        receipt = {
            "pincode": pincode,
            "id": pincode,
            "bytes": len(tar),
            "chunk_bytes": CHUNK_BYTES,
        }
        # OMITTED WHEN UNKNOWN, never sent as false. The daemon refuses an
        # explicit false and passes an absent one, which is the same rule
        # sceneClaimsComplete has always followed on the Go side.
        complete = scene_is_complete(tar)
        if complete is not None:
            receipt["complete"] = complete
        self._json(200, receipt)

    def log_message(self, fmt, *args):
        # The pod's stdout is the bake's log: one line per request, no
        # per-header noise.
        print("region-scene: " + (fmt % args), flush=True)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
