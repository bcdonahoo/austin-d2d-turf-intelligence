"""
KUBRA Storm Center client for Austin Energy's public outage map.

Austin Energy's outage map (https://outagemap.austinenergy.com/) is a KUBRA
Storm Center deployment. KUBRA exposes public JSON endpoints behind the map:

  1. currentState  -> resolves the current data deployment paths (they rotate)
  2. summary       -> total outages / customers affected
  3. serviceareas  -> service territory polygon (Google polyline encoded)
  4. cluster tiles -> per-quadkey outage detail (drill into clusters by zooming)

Endpoint pattern verified against the Open Austin reference implementation:
https://github.com/open-austin/energy-outage

Dependency-light by design: requests only. Quadkey tile math and Google
polyline decoding are implemented inline below.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import requests

BASE_URL = "https://kubra.io/"

# Austin Energy Storm Center identifiers (from the map's HTML config; see
# open-austin/energy-outage). If Austin Energy redeploys, re-extract these
# from the network tab at outagemap.austinenergy.com.
AUSTIN_ENERGY = {
    "instance_id": "dd9c446f-f6b8-43f9-8f80-83f5245c60a1",
    "view_id": "76446308-a901-4fa3-849c-3dd569933a51",
}

MIN_ZOOM = 10  # Austin service area is compact; start deeper than statewide maps
MAX_ZOOM = 14  # KUBRA does not resolve clusters beyond zoom 14


# ---------------------------------------------------------------------------
# Pure-python tile / polyline utilities (replaces mercantile + polyline deps)
# ---------------------------------------------------------------------------

def latlng_to_tile(lat: float, lng: float, zoom: int) -> tuple[int, int]:
    """Web-mercator tile (x, y) containing a lat/lng at a zoom level."""
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2 ** zoom
    x = int((lng + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile_to_quadkey(x: int, y: int, zoom: int) -> str:
    """Bing-style quadkey for a tile."""
    quadkey = []
    for i in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        quadkey.append(str(digit))
    return "".join(quadkey)


def quadkey_to_tile(quadkey: str) -> tuple[int, int, int]:
    x = y = 0
    zoom = len(quadkey)
    for i, c in enumerate(quadkey):
        mask = 1 << (zoom - i - 1)
        d = int(c)
        if d & 1:
            x |= mask
        if d & 2:
            y |= mask
    return x, y, zoom


def latlng_to_quadkey(lat: float, lng: float, zoom: int) -> str:
    x, y = latlng_to_tile(lat, lng, zoom)
    return tile_to_quadkey(x, y, zoom)


def neighboring_quadkeys(quadkey: str) -> list[str]:
    x, y, z = quadkey_to_tile(quadkey)
    n = 2 ** z
    out = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            nx, ny = x + dx, y + dy
            if 0 <= nx < n and 0 <= ny < n:
                out.append(tile_to_quadkey(nx, ny, z))
    return out


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Decode a Google Polyline Algorithm string to [(lat, lng), ...]."""
    points, index, lat, lng = [], 0, 0, 0
    while index < len(encoded):
        for coord in ("lat", "lng"):
            shift, result = 0, 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if coord == "lat":
                lat += delta
            else:
                lng += delta
        points.append((lat / 1e5, lng / 1e5))
    return points


def bbox_quadkeys(points: list[tuple[float, float]], zoom: int) -> list[str]:
    """All quadkeys at `zoom` covering the bounding box of the given points."""
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    x_min, y_min = latlng_to_tile(max(lats), min(lngs), zoom)
    x_max, y_max = latlng_to_tile(min(lats), max(lngs), zoom)
    keys = []
    for x in range(min(x_min, x_max), max(x_min, x_max) + 1):
        for y in range(min(y_min, y_max), max(y_min, y_max) + 1):
            keys.append(tile_to_quadkey(x, y, zoom))
    return keys


# ---------------------------------------------------------------------------
# KUBRA client
# ---------------------------------------------------------------------------

@dataclass
class KubraClient:
    instance_id: str = AUSTIN_ENERGY["instance_id"]
    view_id: str = AUSTIN_ENERGY["view_id"]
    session: requests.Session = field(default_factory=requests.Session)
    request_delay: float = 0.25  # be polite; the map itself refreshes ~10 min

    def __post_init__(self):
        self.session.headers.update(
            {"User-Agent": "outage-history-research/0.1 (contact: local)"}
        )
        self._resolve_state()

    # -- endpoint resolution -------------------------------------------------

    def _get(self, url: str) -> requests.Response:
        time.sleep(self.request_delay)
        return self.session.get(url, timeout=30)

    def _resolve_state(self) -> None:
        """currentState resolves the rotating data-deployment paths."""
        url = (
            f"{BASE_URL}stormcenter/api/v1/stormcenters/"
            f"{self.instance_id}/views/{self.view_id}/currentState?preview=false"
        )
        state = self._get(url).json()
        self.regions_key = list(state["datastatic"])[0]
        self.regions_path = state["datastatic"][self.regions_key]
        self.data_path = state["data"]["interval_generation_data"]
        self.cluster_data_path = state["data"]["cluster_interval_generation_data"]
        deployment_id = state["stormcenterDeploymentId"]

        config_url = (
            f"{BASE_URL}stormcenter/api/v1/stormcenters/"
            f"{self.instance_id}/views/{self.view_id}/configuration/"
            f"{deployment_id}?preview=false"
        )
        config = self._get(config_url).json()
        layers = config["config"]["layers"]["data"]["interval_generation_data"]
        self.layer_name = next(
            l["id"] for l in layers if l["type"].startswith("CLUSTER_LAYER")
        )

    # -- public fetches ------------------------------------------------------

    def fetch_summary(self) -> dict:
        url = f"{BASE_URL}{self.data_path}/public/summary-1/data.json"
        return self._get(url).json()

    def service_area_points(self) -> list[tuple[float, float]]:
        url = f"{BASE_URL}{self.regions_path}/{self.regions_key}/serviceareas.json"
        res = self._get(url).json()
        geoms = res["file_data"][0]["geom"]["a"]
        points: list[tuple[float, float]] = []
        for g in geoms:
            points.extend(decode_polyline(g))
        return points

    def fetch_outages(self) -> list[dict]:
        """Crawl cluster tiles across the service area, drilling into clusters."""
        quadkeys = bbox_quadkeys(self.service_area_points(), MIN_ZOOM)
        outages = self._crawl(quadkeys, set(), MIN_ZOOM)
        return list(outages.values())

    # -- crawl internals -------------------------------------------------------

    def _quadkey_url(self, quadkey: str) -> str:
        # KUBRA shards tile data by the reversed last 3 quadkey digits
        data_path = self.cluster_data_path.format(qkh=quadkey[-3:][::-1])
        return f"{BASE_URL}{data_path}/public/{self.layer_name}/{quadkey}.json"

    def _crawl(self, quadkeys, seen: set, zoom: int) -> dict:
        outages: dict[str, dict] = {}
        for q in quadkeys:
            url = self._quadkey_url(q)
            if url in seen:
                continue
            seen.add(url)
            res = self._get(url)
            if not res.ok:  # no file means no outages in this tile
                continue
            for o in res.json().get("file_data", []):
                if o["desc"]["cluster"] and zoom + 1 <= MAX_ZOOM:
                    lat, lng = decode_polyline(o["geom"]["p"][0])[0]
                    child = latlng_to_quadkey(lat, lng, zoom + 1)
                    outages.update(self._crawl([child], seen, zoom + 1))
                else:
                    rec = self._parse_outage(o, url)
                    outages[rec["incident_id"]] = rec
                    # incidents can straddle tile edges; check neighbors
                    outages.update(
                        self._crawl(neighboring_quadkeys(q), seen, zoom)
                    )
        return outages

    @staticmethod
    def _parse_outage(raw: dict, source_url: str) -> dict:
        desc = raw["desc"]
        lat, lng = decode_polyline(raw["geom"]["p"][0])[0]
        incident_id = (
            desc["inc_id"]
            if desc.get("inc_id")
            else f"{raw['geom']['p'][0]}-{desc['start_time']}"
        )
        cause = desc.get("cause") or {}
        crew_status = desc.get("crew_status") or {}
        return {
            "incident_id": str(incident_id),
            "latitude": lat,
            "longitude": lng,
            "cause": cause.get("EN-US") if isinstance(cause, dict) else cause,
            "num_out": desc.get("n_out"),
            "cust_affected": (desc.get("cust_a") or {}).get("val"),
            "crew_status": crew_status.get("EN-US") if isinstance(crew_status, dict) else crew_status,
            "start_time": desc.get("start_time"),
            "etr": desc.get("etr"),
            "etr_confidence": desc.get("etr_confidence"),
            "is_cluster": bool(desc.get("cluster")),
            "source": source_url,
        }
