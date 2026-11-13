"""One-time builder for the trip map's paths, so the site never calls a
routing service:

- walking_paths.json: the walking path between every pair of trip planner
  points (campus places and bus stops).
- bus_paths.json: for each bus route, its stops in the order the bus visits
  them, its color, and the driving path from each stop to the next.

Run from the scripts folder:  python build_paths.py
The first run takes about 12 minutes (one request per second, as the free
routing server asks). Later runs only fetch missing walking paths, plus the
bus paths (about a minute). Re-run after changing PLACES or when UD changes
its stops or routes. main.py works without the files (straight lines).

Not unit tested: it only talks to web services and writes files.
"""
import ast
import json
import time
import urllib.request

MAIN_FILE = "main.py"
WALKING_FILE = "walking_paths.json"
BUS_FILE = "bus_paths.json"
BUS_API = ("https://udshuttle.etaspot.net/service.php"
           "?service={}&token=TESTING")
ROUTER = ("https://routing.openstreetmap.de/routed-{}/route/v1/{}/"
          "{},{};{},{}?overview=full&geometries=polyline")
HEADERS = {"User-Agent": "cisc106-campus-bus-project/1.0"}
SECONDS_BETWEEN_REQUESTS = 1.0
SAVE_EVERY = 25  # each save makes drafter's dev server reload the site


def read_places() -> dict[str, list[float]]:
    """Reads the PLACES dictionary out of main.py without running main.py."""
    with open(MAIN_FILE, encoding="utf-8") as file:
        tree = ast.parse(file.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == "PLACES":
            return ast.literal_eval(node.value)
    raise ValueError("PLACES not found in " + MAIN_FILE)


def get_json(url: str) -> dict:
    """Downloads and parses one JSON response."""
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def find_path(profile: str, start: list[float],
              end: list[float]) -> list | None:
    """
    Returns [km, encoded path] from start to end for profile "foot" or
    "car", or None if the router fails. Waits between requests.
    """
    mode = "foot" if profile == "foot" else "driving"
    url = ROUTER.format(profile, mode, start[1], start[0], end[1], end[0])
    time.sleep(SECONDS_BETWEEN_REQUESTS)
    try:
        best = get_json(url)["routes"][0]
    except (OSError, ValueError, KeyError, IndexError) as error:
        print("skipped", start, end, error)  # main.py draws a straight line
        return None
    return [round(best["distance"] / 1000, 3), best["geometry"]]


def save(paths: dict, file_name: str) -> None:
    """Writes one paths file."""
    with open(file_name, "w", encoding="utf-8") as file:
        json.dump(paths, file, separators=(",", ":"), sort_keys=True)


def path_key(lat1: float, lng1: float, lat2: float,
             lng2: float) -> tuple[str, bool]:
    """Same key as path_key in main.py; the two must stay identical."""
    first = f"{lat1:.5f},{lng1:.5f}"
    second = f"{lat2:.5f},{lng2:.5f}"
    if first <= second:
        return first + "|" + second, False
    return second + "|" + first, True


def build_walking(points: list[list[float]]) -> None:
    """Fetches every missing walking path and saves walking_paths.json."""
    try:
        with open(WALKING_FILE, encoding="utf-8") as file:
            paths = json.load(file)
    except FileNotFoundError:
        paths = {}
    pairs = [(a, b) for i, a in enumerate(points) for b in points[i + 1:]]
    missing = [(a, b) for a, b in pairs
               if path_key(a[0], a[1], b[0], b[1])[0] not in paths]
    print(len(pairs), "walking pairs,", len(missing), "to fetch")
    for count, (a, b) in enumerate(missing, start=1):
        key, flipped = path_key(a[0], a[1], b[0], b[1])
        path = find_path("foot", *((b, a) if flipped else (a, b)))
        if path is not None:
            paths[key] = path
        if count % SAVE_EVERY == 0:
            save(paths, WALKING_FILE)
            print(count, "/", len(missing))
    save(paths, WALKING_FILE)


def build_bus(stops_by_id: dict[int, list[float]]) -> None:
    """
    Saves bus_paths.json: {route name: {"color", "stops", "legs"}}, where
    legs[i] is the driving path from stops[i] to the next stop in the loop.
    Uses each route's first pattern; a route's patterns share one loop.
    """
    routes: dict[str, dict] = {}
    for pattern in get_json(BUS_API.format("get_patterns"))["get_patterns"]:
        name = pattern["routeNames"][0]
        if name in routes:
            continue
        # the loop repeats its first stop at the end; drop unknown stops
        stops = [stop for stop in pattern["stopIDs"][:-1]
                 if stop in stops_by_id]
        legs = [find_path("car", stops_by_id[stop],
                          stops_by_id[stops[(i + 1) % len(stops)]])
                for i, stop in enumerate(stops)]
        routes[name] = {"color": pattern["color"], "stops": stops,
                        "legs": legs}
        print(name, len(stops), "stops")
    save(routes, BUS_FILE)


def main() -> None:
    """Builds both files."""
    stops_by_id = {}
    for stop in get_json(BUS_API.format("get_stops"))["get_stops"]:
        stops_by_id[stop["id"]] = [stop["lat"], stop["lng"]]
    build_bus(stops_by_id)
    build_walking(list(read_places().values()) + list(stops_by_id.values()))


if __name__ == "__main__":
    main()
