import logging
from io import BytesIO

from staticmap import CircleMarker, Line, StaticMap

logger = logging.getLogger("run-split-tracker")

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# OpenStreetMap's tile usage policy asks clients to identify themselves.
TILE_HEADERS = {"User-Agent": "run-split-tracker/1.0 (personal run tracker)"}


def render_route_png(path, width=800, height=500):
    """PNG bytes of a route [(lat, lon), ...] drawn like the dashboard map, or None on failure."""
    if not path:
        return None
    try:
        image_map = StaticMap(
            width, height, padding_x=40, padding_y=40,
            url_template=TILE_URL, headers=TILE_HEADERS, tile_request_timeout=5,
        )
        coords = [(lon, lat) for lat, lon in path]  # staticmap takes (lon, lat)
        if len(coords) > 1:
            image_map.add_line(Line(coords, "#0d6efd", 5))
        image_map.add_marker(CircleMarker(coords[-1], "white", 18))
        image_map.add_marker(CircleMarker(coords[-1], "#dc3545", 12))
        buffer = BytesIO()
        image_map.render().save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as exc:
        logger.warning("Map image: render failed (%s: %s)", type(exc).__name__, exc)
        return None
