"""UD Bus Tracker: live UD bus wait times and a walk-or-ride trip planner.

Drafter docs: https://drafter-edu.github.io/drafter/
Bus data: the public ETA SPOT JSON service behind udshuttle.etaspot.net.
"""
import html
import json
import math
import sys
import time
from dataclasses import dataclass, field
from drafter import *  # type: ignore

API_URL = "https://udshuttle.etaspot.net/service.php?service={}&token=TESTING"
UD_CENTER = (39.6800, -75.7520)
UD_BLUE = "#00539F"
UD_GOLD = "#FFD200"
WALKING_KM_PER_HOUR = 5.0
EARTH_RADIUS_KM = 6371.0
TOP_COUNT = 5
NO_BUS = -1
MAX_DATA_AGE_SECONDS = 60  # API minutes count from the load, so they go stale
LEAFLET_URL = "https://unpkg.com/leaflet@1.9.4/dist/leaflet"
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# Drafter puts this file inside an HTML script tag, so the literal closing
# tag must never appear in the source, or the browser cuts the code off.
SCRIPT_END = "</" + "script>"

# shortcut: hand-picked approximate coordinates, swap in a campus GIS file
# if the walking estimates need to be more precise.
PLACES = {
    "Morris Library": (39.6797, -75.7522),
    "Trabant University Center": (39.6827, -75.7543),
    "Perkins Student Center": (39.6858, -75.7482),
    "Memorial Hall (The Green)": (39.6787, -75.7520),
    "ISE Lab": (39.6807, -75.7498),
    "Carpenter Sports Building": (39.6769, -75.7509),
    "Main Street (Newark)": (39.6836, -75.7497),
    "Laird Campus": (39.6881, -75.7566),
    "STAR Campus": (39.6671, -75.7520),
    "Bob Carpenter Center": (39.6634, -75.7490),
    "Delaware Stadium": (39.6625, -75.7473),
}


@dataclass
class Arrival:
    route_name: str
    vehicle_block: int
    minutes: int


@dataclass
class Stop:
    stop_id: int
    name: str
    latitude: float
    longitude: float
    arrivals: list[Arrival]


@dataclass
class RouteLine:
    color: str
    points: list[list[float]]


@dataclass
class TripPlan:
    walk_only_minutes: int
    bus_total_minutes: int
    route_name: str
    board_stop: Stop | None
    exit_stop: Stop | None
    walk_to_stop_minutes: int
    bus_arrives_in: int
    ride_minutes: int
    walk_from_stop_minutes: int


@dataclass
class State:
    stops: list[Stop]
    running_routes: list[str]
    not_running_routes: list[str]
    origin: str
    destination: str
    skip_request_only: bool
    status_message: str
    loaded_at: float
    route_lines: dict[str, RouteLine] = field(default_factory=dict)


# ---------------------------------------------------------------- data


def fetch_service(service: str) -> list[dict]:
    """
    Downloads one ETA SPOT service (e.g. "get_stops") and returns its list.

    Not unit tested: it depends on the live network.
    """
    url = API_URL.format(service)
    if sys.platform == "emscripten":
        # Drafter runs this file in the browser (Pyodide), where urllib
        # cannot open sockets; open_url uses the browser's own request.
        from pyodide.http import open_url  # type: ignore
        text = open_url(url).read()
    else:
        from urllib.request import urlopen
        with urlopen(url, timeout=15) as response:
            text = response.read().decode("utf-8")
    return json.loads(text)[service]


def build_stops(routes_data: list[dict], stops_data: list[dict],
                etas_data: list[dict]) -> list[Stop]:
    """
    Turns the raw API lists into Stop objects with their upcoming arrivals.

    The stops service repeats a stop once per route, so stops are merged by
    id. Arrivals are sorted soonest first.
    """
    route_names = {route["id"]: route["name"] for route in routes_data}
    stops_by_id: dict[int, Stop] = {}
    for row in stops_data:
        if row["id"] not in stops_by_id:
            stops_by_id[row["id"]] = Stop(row["id"], row["name"], row["lat"],
                                          row["lng"], [])
    for eta in etas_data:
        stop = stops_by_id.get(eta["id"])
        if stop is not None:
            for bus in eta.get("enRoute", []):
                name = route_names.get(bus["routeID"], "Unknown route")
                stop.arrivals.append(
                    Arrival(name, bus["blockID"], bus["minutes"]))
            stop.arrivals.sort(key=lambda arrival: arrival.minutes)
    return list(stops_by_id.values())


def decode_polyline(encoded: str) -> list[list[float]]:
    """
    Decodes a Google "encoded polyline" (how the API stores route shapes)
    into [latitude, longitude] points.

    Each number is stored as the change from the previous point, 5 bits per
    character with 1e-5 degree precision.
    """
    points: list[list[float]] = []
    index = 0
    latitude = 0
    longitude = 0
    while index < len(encoded):
        deltas = []
        for _ in range(2):  # one latitude change, then one longitude change
            result = 0
            shift = 0
            chunk = 0x20
            while chunk >= 0x20 and index < len(encoded):
                chunk = ord(encoded[index]) - 63
                index += 1
                result |= (chunk & 0x1F) << shift
                shift += 5
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += deltas[0]
        longitude += deltas[1]
        points.append([latitude / 1e5, longitude / 1e5])
    return points


def build_route_lines(routes_data: list[dict]) -> dict[str, RouteLine]:
    """Returns each route's map color and road shape, keyed by route name."""
    lines = {}
    for route in routes_data:
        lines[route["name"]] = RouteLine(
            route.get("color", UD_BLUE),
            decode_polyline(route.get("encLine", "")))
    return lines


def routes_with_arrivals(stops: list[Stop]) -> list[str]:
    """Returns the names of routes that have a bus coming to any stop."""
    served = []
    for stop in stops:
        for arrival in stop.arrivals:
            if arrival.route_name not in served:
                served.append(arrival.route_name)
    return served


def load_bus_data(state: State) -> State:
    """
    Refreshes the stops and route lists in the state from the live API.

    Not unit tested: it depends on the live network. On failure the old data
    is kept and the error is shown to the user.
    """
    state.loaded_at = time.time()  # also on failure, so retries wait a bit
    try:
        routes_data = fetch_service("get_routes")
        stops = build_stops(routes_data, fetch_service("get_stops"),
                            fetch_service("get_stop_etas"))
    except Exception as error:  # network, JSON or browser errors alike
        state.status_message = "Could not load live bus data: " + str(error)
        return state
    served = routes_with_arrivals(stops)
    all_routes = [route["name"] for route in routes_data]
    state.stops = stops
    state.route_lines = build_route_lines(routes_data)
    state.running_routes = [name for name in all_routes if name in served]
    state.not_running_routes = [name for name in all_routes
                                if name not in served]
    state.status_message = ("Live data loaded: " + str(len(stops))
                            + " stops on " + str(len(all_routes)) + " routes.")
    return state


def refresh_if_stale(state: State) -> State:
    """
    Reloads live data when it is older than MAX_DATA_AGE_SECONDS.

    Only the fresh case is unit tested; the stale case uses the network.
    """
    if time.time() - state.loaded_at > MAX_DATA_AGE_SECONDS:
        return load_bus_data(state)
    return state


# ------------------------------------------------------------- ranking


def rank_stops_by_wait(stops: list[Stop], slowest: bool) -> list[Stop]:
    """
    Returns up to TOP_COUNT stops ordered by their next bus, fastest first
    (or slowest first). Stops with no bus coming are left out.
    """
    waiting = [stop for stop in stops if stop.arrivals]
    waiting.sort(key=lambda stop: stop.arrivals[0].minutes, reverse=slowest)
    return waiting[:TOP_COUNT]


# ---------------------------------------------------------- trip math


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Returns the straight-line (haversine) distance between two points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def walking_minutes(lat1: float, lng1: float, lat2: float,
                    lng2: float) -> int:
    """
    Returns whole minutes to walk between two points, rounded up.

    shortcut: straight-line distance ignores paths and buildings, so it
    underestimates; use a routing service if that matters.
    """
    km = distance_km(lat1, lng1, lat2, lng2)
    return math.ceil(km / WALKING_KM_PER_HOUR * 60)


def is_request_only(stop: Stop) -> bool:
    """Returns True for stops the bus only serves when requested."""
    return "request only" in stop.name.lower()


def usable_stops(stops: list[Stop], skip_request_only: bool) -> list[Stop]:
    """Returns the stops the rider is willing to use."""
    if skip_request_only:
        return [stop for stop in stops if not is_request_only(stop)]
    return stops


def nearest_stop(latitude: float, longitude: float,
                 stops: list[Stop]) -> Stop | None:
    """Returns the stop closest to a point, or None if there are no stops."""
    if not stops:
        return None
    return min(stops, key=lambda stop: distance_km(
        latitude, longitude, stop.latitude, stop.longitude))


def later_arrival(stop: Stop, vehicle_block: int,
                  after_minutes: int) -> Arrival | None:
    """Returns the first time a given bus reaches a stop after a time."""
    for arrival in stop.arrivals:
        if (arrival.vehicle_block == vehicle_block
                and arrival.minutes > after_minutes):
            return arrival
    return None


def plan_trip(origin: tuple[float, float], destination: tuple[float, float],
              stops: list[Stop]) -> TripPlan:
    """
    Compares walking straight to the destination against riding the bus
    from the stop nearest the origin.

    Every catchable bus at the boarding stop is followed to every other stop
    it reaches later, and the fastest door-to-door option is kept.
    bus_total_minutes is NO_BUS when no bus helps.
    """
    walk_only = walking_minutes(*origin, *destination)
    plan = TripPlan(walk_only, NO_BUS, "", None, None, 0, 0, 0, 0)
    board = nearest_stop(*origin, stops)
    if board is None:
        return plan
    plan.board_stop = board
    walk_to = walking_minutes(*origin, board.latitude, board.longitude)
    plan.walk_to_stop_minutes = walk_to
    for boarding in board.arrivals:
        if boarding.minutes >= walk_to:  # the rider can make this bus
            for exit_stop in stops:
                alighting = later_arrival(exit_stop, boarding.vehicle_block,
                                          boarding.minutes)
                if exit_stop is not board and alighting is not None:
                    walk_from = walking_minutes(exit_stop.latitude,
                                                exit_stop.longitude,
                                                *destination)
                    total = alighting.minutes + walk_from
                    if plan.bus_total_minutes == NO_BUS \
                            or total < plan.bus_total_minutes:
                        plan.bus_total_minutes = total
                        plan.route_name = boarding.route_name
                        plan.exit_stop = exit_stop
                        plan.bus_arrives_in = boarding.minutes
                        plan.ride_minutes = (alighting.minutes
                                             - boarding.minutes)
                        plan.walk_from_stop_minutes = walk_from
    return plan


def recommendation(plan: TripPlan) -> str:
    """Returns a one-sentence walk-or-ride verdict for a trip plan."""
    if plan.bus_total_minutes == NO_BUS:
        return ("No bus gets you there right now. Walk: about "
                + str(plan.walk_only_minutes) + " min.")
    if plan.walk_only_minutes <= plan.bus_total_minutes:
        return ("Just walk! About " + str(plan.walk_only_minutes)
                + " min, vs " + str(plan.bus_total_minutes) + " min by bus.")
    return ("Take the " + plan.route_name + " bus: about "
            + str(plan.bus_total_minutes) + " min, vs "
            + str(plan.walk_only_minutes) + " min walking.")


# ----------------------------------------------------------- page parts


def nav_bar() -> Div:
    """Returns the navigation buttons shown at the top of every page."""
    return Div(Button("Home", index), Button("Fastest stops", fastest_stops),
               Button("Slowest stops", slowest_stops),
               Button("Route status", route_status),
               Button("Plan a trip", trip_planner),
               Button("Refresh", refresh))


def wait_table(stops: list[Stop]) -> Table | Text:
    """Returns a table of stops and their next bus, or a notice if empty."""
    if not stops:
        return Text("No buses are on their way right now.")
    rows: list[list[Content]] = [
        [str(rank + 1), stop.name, stop.arrivals[0].route_name,
         str(stop.arrivals[0].minutes) + " min"]
        for rank, stop in enumerate(stops)]
    return Table(rows, header=["#", "Stop", "Next bus", "Arrives in"])


def place_coordinates(stops: list[Stop]) -> dict[str, tuple[float, float]]:
    """
    Returns every place a rider can pick, by the name shown in the lists:
    the campus places, then each bus stop as "Bus stop: <name>".
    """
    coordinates = dict(PLACES)
    for stop in stops:
        coordinates["Bus stop: " + stop.name] = (stop.latitude,
                                                 stop.longitude)
    return coordinates


def planner_markers(stops: list[Stop]) -> list[MapMarker]:
    """
    Returns a pin for every choice in the planner's lists, labeled with the
    same name as in the lists (bus stops also show their next bus).
    """
    markers = [MapMarker(lat, lng, name)
               for name, (lat, lng) in PLACES.items()]
    for stop in stops:
        label = "Bus stop: " + stop.name
        if stop.arrivals:
            label += " (next bus " + str(stop.arrivals[0].minutes) + " min)"
        markers.append(MapMarker(stop.latitude, stop.longitude, label))
    return markers


def trip_details(plan: TripPlan, destination: str) -> list:
    """Returns numbered, plain-language directions for a trip plan."""
    walk_step = ("Walk straight to " + destination + ". It takes about "
                 + str(plan.walk_only_minutes) + " min.")
    if plan.board_stop is None or plan.exit_stop is None:
        return [NumberedList([walk_step])]
    bus_steps = NumberedList([
        "Walk " + str(plan.walk_to_stop_minutes) + " min to the bus stop "
        + plan.board_stop.name + ".",
        "Wait for the " + plan.route_name + " bus. It arrives in "
        + str(plan.bus_arrives_in) + " min, so you wait about "
        + str(plan.bus_arrives_in - plan.walk_to_stop_minutes) + " min.",
        "Ride " + str(plan.ride_minutes) + " min and get off at "
        + plan.exit_stop.name + ".",
        "Walk " + str(plan.walk_from_stop_minutes) + " min to "
        + destination + ".",
    ])
    bus_total = "Total by bus: about " + str(plan.bus_total_minutes) + " min."
    if plan.walk_only_minutes <= plan.bus_total_minutes:
        return [NumberedList([walk_step]),
                Header("Or, if you'd rather take the bus:", 3),
                bus_steps, Text(bus_total)]
    return [bus_steps, Text(bus_total + " Walking the whole way takes about "
                            + str(plan.walk_only_minutes) + " min.")]


def trip_map_html(markers: list[list], walks: list[list[list[float]]],
                  route_line: RouteLine | None) -> str:
    """
    Returns an embedded Leaflet map (an iframe) showing pins, dashed walking
    lines and an optional bus route line, zoomed to the pins and walks.

    markers are [latitude, longitude, label]; walks are lists of points.
    """
    data = {"markers": markers, "walks": walks,
            "route": None if route_line is None else
            {"color": route_line.color, "points": route_line.points}}
    # "<" escaped so stop names from the API can't close the <script> tag
    data_json = json.dumps(data).replace("<", "\\u003c")
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="{LEAFLET_URL}.css">
<script src="{LEAFLET_URL}.js">{SCRIPT_END}
<style>html, body, #map {{ height: 100%; margin: 0; }}</style>
</head><body><div id="map"></div><script>
var data = {data_json};
var map = L.map("map");
L.tileLayer("{TILE_URL}",
  {{attribution: "&copy; OpenStreetMap contributors"}}).addTo(map);
if (data.route) {{
  L.polyline(data.route.points,
    {{color: data.route.color, weight: 5, opacity: 0.6}}).addTo(map);
}}
var focus = [];
data.walks.forEach(function (walk) {{
  focus.push(L.polyline(walk, {{color: "#1a1a1a", weight: 4,
                                dashArray: "6 8"}}));
}});
data.markers.forEach(function (pin) {{
  focus.push(L.marker([pin[0], pin[1]])
    .bindTooltip(pin[2], {{permanent: true, direction: "top"}}));
}});
var group = L.featureGroup(focus).addTo(map);
map.fitBounds(group.getBounds(), {{padding: [40, 40]}});
{SCRIPT_END}</body></html>"""
    return ('<iframe title="Trip map" style="width: 100%; height: 400px; '
            'border: 0; border-radius: 12px;" srcdoc="'
            + html.escape(page, quote=True) + '"></iframe>')


def trip_map(plan: TripPlan, origin: str, destination: str,
             places: dict[str, tuple[float, float]],
             route_lines: dict[str, RouteLine]) -> list:
    """
    Returns the trip map and its legend. The map draws what the verdict
    says: the bus route and walks to and from it, or the direct walk.
    """
    start, end = list(places[origin]), list(places[destination])
    markers = [start + ["Start: " + origin], end + ["End: " + destination]]
    board, exit_stop = plan.board_stop, plan.exit_stop
    if board is None or exit_stop is None \
            or plan.walk_only_minutes <= plan.bus_total_minutes:
        return [RawHTML(trip_map_html(markers, [[start, end]], None)),
                Text("Dashed line: walk (straight-line).")]
    board_point = [board.latitude, board.longitude]
    exit_point = [exit_stop.latitude, exit_stop.longitude]
    markers += [board_point + ["Get on: " + board.name],
                exit_point + ["Get off: " + exit_stop.name]]
    walks = [[start, board_point], [exit_point, end]]
    return [RawHTML(trip_map_html(markers, walks,
                                  route_lines.get(plan.route_name))),
            Text("Colored line: the " + plan.route_name
                 + " bus route. Dashed lines: walks (straight-line).")]


# --------------------------------------------------------------- routes


@route
def index(state: State) -> Page:
    """Home page: data status and a summary of what is running."""
    state = refresh_if_stale(state)
    return Page(state, [
        nav_bar(),
        Header("UD Bus Tracker"),
        Text(state.status_message),
        Paragraph(str(len(state.running_routes)) + " routes running, "
                  + str(len(state.not_running_routes)) + " not running."),
        Paragraph("See which stops have the shortest and longest waits, "
                  "or plan a trip to find out if walking beats the bus."),
    ])


@route
def refresh(state: State) -> Page:
    """
    Reloads live bus data and returns to the home page.

    Not unit tested: it depends on the live network.
    """
    return index(load_bus_data(state))


@route
def fastest_stops(state: State) -> Page:
    """Shows the stops with the shortest wait for the next bus."""
    state = refresh_if_stale(state)
    return Page(state, [
        nav_bar(), Header("Top 5 fastest waits"),
        wait_table(rank_stops_by_wait(state.stops, False)),
    ])


@route
def slowest_stops(state: State) -> Page:
    """Shows the stops with the longest wait for the next bus."""
    state = refresh_if_stale(state)
    return Page(state, [
        nav_bar(), Header("Top 5 slowest waits"),
        wait_table(rank_stops_by_wait(state.stops, True)),
    ])


@route
def route_status(state: State) -> Page:
    """Lists which routes have buses on the way and which do not."""
    state = refresh_if_stale(state)
    running: list = [Text("None.")]
    not_running: list = [Text("None.")]
    if state.running_routes:
        running = [BulletedList(state.running_routes)]
    if state.not_running_routes:
        not_running = [BulletedList(state.not_running_routes)]
    return Page(state, [
        nav_bar(), Header("Route status"), Text(state.status_message),
        Header("Running", 2), *running,
        Header("Not running", 2), *not_running,
    ])


@route
def trip_planner(state: State) -> Page:
    """Form to pick a start and destination, with a pin for each choice."""
    state = refresh_if_stale(state)
    place_names = list(place_coordinates(state.stops))
    origin = state.origin if state.origin in place_names else place_names[0]
    destination = state.destination if state.destination in place_names \
        else place_names[1]
    origin_box = SelectBox("origin", place_names, origin)
    destination_box = SelectBox("destination", place_names, destination)
    skip_box = CheckBox("skip_request_only", state.skip_request_only)
    return Page(state, [
        nav_bar(), Header("Plan a trip"),
        NumberedList(["Find where you are and where you're going on the "
                      "map. Hover over a pin to see its name.",
                      "Pick the same names in the two lists below.",
                      "Press \"Find my trip\" to see if walking or the bus "
                      "is faster, with step-by-step directions."]),
        Map("stops_map", center=UD_CENTER, zoom=14,
            markers=planner_markers(state.stops), height=400),
        Label("I am at:", origin_box), origin_box,
        Label("I am going to:", destination_box), destination_box,
        skip_box, Label("Skip request-only stops", skip_box),
        Button("Find my trip", find_trip),
    ])


@route
def find_trip(state: State, origin: str, destination: str,
              skip_request_only: bool) -> Page:
    """Saves the trip choices and shows whether to walk or take the bus."""
    state = refresh_if_stale(state)
    places = place_coordinates(state.stops)
    if origin not in places or destination not in places:
        return Page(state, [nav_bar(), Header("Unknown place"),
                            Button("Back", trip_planner)])
    state.origin = origin
    state.destination = destination
    state.skip_request_only = skip_request_only
    if origin == destination:
        return Page(state, [nav_bar(), Header("You are already there!"),
                            Button("Plan another trip", trip_planner)])
    stops = usable_stops(state.stops, skip_request_only)
    plan = plan_trip(places[origin], places[destination], stops)
    return Page(state, [
        nav_bar(), Header(origin + " to " + destination),
        Text(state.status_message),
        Header(recommendation(plan), 2),
        *trip_details(plan, destination),
        *trip_map(plan, origin, destination, places, state.route_lines),
        Button("Plan another trip", trip_planner),
    ])


# ---------------------------------------------------------------- tests
# Small hand-made samples shaped like the real API responses.

SAMPLE_ROUTES = [{"id": 1, "name": "College Avenue"},
                 {"id": 8, "name": "Academy Street"}]
SAMPLE_STOPS_DATA = [
    {"id": 1, "name": "North Stop", "lat": 39.6900, "lng": -75.7520},
    {"id": 2, "name": "South Stop", "lat": 39.6650, "lng": -75.7500},
    {"id": 1, "name": "North Stop", "lat": 39.6900, "lng": -75.7520},
    {"id": 3, "name": "Lot 6 - Request Only", "lat": 39.6800,
     "lng": -75.7600},
]
SAMPLE_ETAS = [
    {"id": 1, "enRoute": [{"routeID": 1, "blockID": 20, "minutes": 9},
                          {"routeID": 1, "blockID": 21, "minutes": 3}]},
    {"id": 2, "enRoute": [{"routeID": 1, "blockID": 21, "minutes": 8}]},
    {"id": 3, "enRoute": []},
]


def sample_stops() -> list[Stop]:
    """Returns fresh sample stops for tests (tests may modify them)."""
    return build_stops(SAMPLE_ROUTES, SAMPLE_STOPS_DATA, SAMPLE_ETAS)


TEST_TIME = time.time()  # "just loaded", so routes never hit the network


def sample_state() -> State:
    """Returns a fresh sample state for tests."""
    return State(sample_stops(), ["College Avenue"], ["Academy Street"],
                 "", "", False, "Live data loaded.", TEST_TIME)


NORTH = Stop(1, "North Stop", 39.69, -75.752,
             [Arrival("College Avenue", 21, 3),
              Arrival("College Avenue", 20, 9)])
SOUTH = Stop(2, "South Stop", 39.665, -75.75,
             [Arrival("College Avenue", 21, 8)])
REQUEST = Stop(3, "Lot 6 - Request Only", 39.68, -75.76, [])

# build_stops: merges duplicate stop rows and sorts arrivals
assert_equal(sample_stops(), [NORTH, SOUTH, REQUEST])
assert_equal(build_stops([], [], []), [])
assert_equal(build_stops(SAMPLE_ROUTES, SAMPLE_STOPS_DATA[:1],
                         [{"id": 1, "enRoute": [
                             {"routeID": 99, "blockID": 1, "minutes": 2}]}]),
             [Stop(1, "North Stop", 39.69, -75.752,
                   [Arrival("Unknown route", 1, 2)])])

# routes_with_arrivals
assert_equal(routes_with_arrivals(sample_stops()), ["College Avenue"])
assert_equal(routes_with_arrivals([REQUEST]), [])

# rank_stops_by_wait: skips stops with no bus, caps at TOP_COUNT
assert_equal(rank_stops_by_wait(sample_stops(), False), [NORTH, SOUTH])
assert_equal(rank_stops_by_wait(sample_stops(), True), [SOUTH, NORTH])
assert_equal(len(rank_stops_by_wait([NORTH] * 8, False)), TOP_COUNT)
assert_equal(rank_stops_by_wait([], True), [])

# distance_km and walking_minutes
assert_equal(distance_km(39.68, -75.75, 39.68, -75.75), 0.0)
assert_equal(distance_km(0, 0, 1, 0), 111.19, precision=2)
assert_equal(walking_minutes(39.68, -75.75, 39.68, -75.75), 0)
assert_equal(walking_minutes(0, 0, 0.009, 0), 13)  # about 1 km

# is_request_only and usable_stops
assert_equal(is_request_only(REQUEST), True)
assert_equal(is_request_only(NORTH), False)
assert_equal(usable_stops(sample_stops(), True), [NORTH, SOUTH])
assert_equal(usable_stops(sample_stops(), False), [NORTH, SOUTH, REQUEST])

# nearest_stop
assert_equal(nearest_stop(39.689, -75.752, sample_stops()), NORTH)
assert_equal(nearest_stop(39.68, -75.759, sample_stops()), REQUEST)
assert_equal(nearest_stop(39.68, -75.75, []), None)

# later_arrival
assert_equal(later_arrival(SOUTH, 21, 3), Arrival("College Avenue", 21, 8))
assert_equal(later_arrival(SOUTH, 21, 8), None)
assert_equal(later_arrival(SOUTH, 20, 0), None)

# plan_trip: North -> South rides block 21 (catchable at 3 min, walk 0)
assert_equal(plan_trip((39.69, -75.752), (39.665, -75.75), [NORTH, SOUTH]),
             TripPlan(walking_minutes(39.69, -75.752, 39.665, -75.75), 8,
                      "College Avenue", NORTH, SOUTH, 0, 3, 5, 0))
# no stops at all: walking is the only option
assert_equal(plan_trip((39.69, -75.752), (39.665, -75.75), []),
             TripPlan(34, NO_BUS, "", None, None, 0, 0, 0, 0))
# the only bus leaves before the rider can walk to the stop
# (start is 1 km north of North Stop: a 13 min walk)
assert_equal(plan_trip((39.6990, -75.752), (39.665, -75.75),
                       [NORTH, SOUTH]).bus_total_minutes, NO_BUS)

# recommendation: covers all three verdicts
assert_equal(recommendation(TripPlan(10, NO_BUS, "", None, None,
                                     0, 0, 0, 0)),
             "No bus gets you there right now. Walk: about 10 min.")
assert_equal(recommendation(TripPlan(5, 9, "College Avenue", NORTH, SOUTH,
                                     0, 3, 5, 1)),
             "Just walk! About 5 min, vs 9 min by bus.")
assert_equal(recommendation(TripPlan(30, 9, "College Avenue", NORTH, SOUTH,
                                     0, 3, 5, 1)),
             "Take the College Avenue bus: about 9 min, vs 30 min walking.")

# decode_polyline: Google's documented example, and empty input
assert_equal(decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@"),
             [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]],
             precision=5)
assert_equal(decode_polyline(""), [])

# build_route_lines: uses the API color, falls back to UD blue
assert_equal(build_route_lines([{"name": "College Avenue",
                                 "color": "#1C0CF2",
                                 "encLine": "_p~iF~ps|U"}]),
             {"College Avenue": RouteLine("#1C0CF2", [[38.5, -120.2]])})
assert_equal(build_route_lines([{"name": "Academy Street"}]),
             {"Academy Street": RouteLine(UD_BLUE, [])})

# place_coordinates: campus places plus every stop, by list name
assert_equal(place_coordinates([NORTH])["Bus stop: North Stop"],
             (39.69, -75.752))
assert_equal(len(place_coordinates([NORTH, SOUTH])), len(PLACES) + 2)
assert_equal(place_coordinates([]), PLACES)

# planner_markers: one pin per list choice, with matching names
assert_equal(planner_markers([NORTH, REQUEST])[0],
             MapMarker(*PLACES["Morris Library"], "Morris Library"))
assert_equal(planner_markers([NORTH, REQUEST])[len(PLACES):],
             [MapMarker(39.69, -75.752,
                        "Bus stop: North Stop (next bus 3 min)"),
              MapMarker(39.68, -75.76, "Bus stop: Lot 6 - Request Only")])

# trip_map_html: data is embedded, and "<" can't break out of the script
assert_equal("#1C0CF2" in trip_map_html([], [], RouteLine("#1C0CF2", [])),
             True)
assert_equal("\\u003c/script&gt;" in trip_map_html([[1.0, 2.0, SCRIPT_END]],
                                                   [], None),
             True)

# trip_map: draws the direct walk, or the bus route with walks to and from it
assert_has(trip_map(TripPlan(5, NO_BUS, "", None, None, 0, 0, 0, 0),
                    "ISE Lab", "Morris Library", PLACES, {}),
           "Dashed line: walk (straight-line).")
assert_has(trip_map(TripPlan(30, 9, "College Avenue", NORTH, SOUTH,
                             1, 3, 5, 1),
                    "Laird Campus", "Delaware Stadium", PLACES,
                    {"College Avenue": RouteLine("#1C0CF2", [])}),
           "Colored line: the College Avenue bus route. "
           "Dashed lines: walks (straight-line).")

# nav_bar
assert_has(nav_bar(), Button("Plan a trip", trip_planner))
assert_has(nav_bar(), Button("Refresh", refresh))

# wait_table: a table, or a notice when there is nothing to show
assert_equal(wait_table([SOUTH]),
             Table([["1", "South Stop", "College Avenue", "8 min"]],
                   header=["#", "Stop", "Next bus", "Arrives in"]))
assert_equal(wait_table([]), Text("No buses are on their way right now."))

# trip_details: bus steps when the bus wins, walk first when walking wins
assert_text(trip_details(TripPlan(30, 9, "College Avenue", NORTH, SOUTH,
                                  1, 3, 5, 1), "Delaware Stadium"),
            "Walk 1 min to the bus stop North Stop.\n"
            "Wait for the College Avenue bus. It arrives in 3 min, so you "
            "wait about 2 min.\n"
            "Ride 5 min and get off at South Stop.\n"
            "Walk 1 min to Delaware Stadium.\n"
            "Total by bus: about 9 min. Walking the whole way takes about "
            "30 min.")
assert_has(trip_details(TripPlan(5, 9, "College Avenue", NORTH, SOUTH,
                                 1, 3, 5, 1), "ISE Lab"),
           "Or, if you'd rather take the bus:")
assert_equal(trip_details(TripPlan(10, NO_BUS, "", None, None, 0, 0, 0, 0),
                          "ISE Lab"),
             [NumberedList(["Walk straight to ISE Lab. It takes about "
                            "10 min."])])

# route: index
assert_has(index(sample_state()), "1 routes running, 1 not running.")
assert_has(index(sample_state()), "Live data loaded.")

# route: fastest_stops / slowest_stops
assert_has(fastest_stops(sample_state()), "Top 5 fastest waits")
assert_has(fastest_stops(sample_state()),
           Table([["1", "North Stop", "College Avenue", "3 min"],
                  ["2", "South Stop", "College Avenue", "8 min"]],
                 header=["#", "Stop", "Next bus", "Arrives in"]))
assert_has(slowest_stops(sample_state()),
           Table([["1", "South Stop", "College Avenue", "8 min"],
                  ["2", "North Stop", "College Avenue", "3 min"]],
                 header=["#", "Stop", "Next bus", "Arrives in"]))
assert_has(slowest_stops(State([REQUEST], [], [], "", "", False, "",
                               TEST_TIME)),
           "No buses are on their way right now.")

# refresh_if_stale: fresh data is left alone
assert_state(refresh_if_stale(sample_state()), sample_state())

# route: route_status
assert_has(route_status(sample_state()), BulletedList(["Academy Street"]))
assert_has(route_status(sample_state()), "Live data loaded.")
assert_text(route_status(State([], [], [], "", "", False, "No data.",
                               TEST_TIME)).content[1:],
            "Route status\nNo data.\nRunning\nNone.\nNot running\nNone.")

# route: trip_planner remembers earlier choices
assert_has(trip_planner(State(sample_stops(), [], [], "ISE Lab", "", False,
                              "", TEST_TIME)),
           SelectBox("origin", list(place_coordinates(sample_stops())),
                     "ISE Lab"))
# a remembered place that no longer exists falls back to the first choice
assert_has(trip_planner(State(sample_stops(), [], [], "Gone", "", False,
                              "", TEST_TIME)),
           SelectBox("origin", list(place_coordinates(sample_stops())),
                     "Morris Library"))
assert_has(trip_planner(sample_state()), Button("Find my trip", find_trip))

# route: find_trip saves the choices to state
assert_state(find_trip(sample_state(), "Laird Campus", "Delaware Stadium",
                       True),
             State(sample_stops(), ["College Avenue"], ["Academy Street"],
                   "Laird Campus", "Delaware Stadium", True,
                   "Live data loaded.", TEST_TIME))
assert_has(find_trip(sample_state(), "Laird Campus", "Delaware Stadium",
                     True),
           "Laird Campus to Delaware Stadium")
assert_has(find_trip(sample_state(), "ISE Lab", "Morris Library", False),
           "Live data loaded.")
assert_has(find_trip(sample_state(), "ISE Lab", "ISE Lab", False),
           "You are already there!")
# bus stops from the map can be picked too
assert_has(find_trip(sample_state(), "Bus stop: North Stop",
                     "Bus stop: South Stop", False),
           "Take the College Avenue bus: about 8 min, vs 34 min walking.")
assert_has(find_trip(sample_state(), "Mars", "ISE Lab", False),
           "Unknown place")
assert_state(find_trip(sample_state(), "Mars", "ISE Lab", False),
             sample_state())

# ----------------------------------------------------------------- site

set_site_information(
    author="UD CISC 106 student",
    description="Live UD bus wait times, route status, and a trip planner "
                "that tells you whether to walk or take the bus.",
    sources=["https://udshuttle.etaspot.net (UD bus ETA SPOT data)",
             "https://drafter-edu.github.io/drafter/"],
    planning=["README", "prompt_log.txt"],
    links=["https://github.com/brooksevans1025-droid/Cisc106finalProject"],
)
add_website_css(f"""
body {{ font-family: system-ui, sans-serif; background: #f4f7fb;
       color: #1a1a1a; max-width: 900px; margin: 0 auto; padding: 16px; }}
h1 {{ color: {UD_BLUE}; border-bottom: 4px solid {UD_GOLD};
     padding-bottom: 8px; }}
h2 {{ color: {UD_BLUE}; }}
button {{ background: {UD_BLUE}; color: white; border: none;
         border-radius: 999px; padding: 8px 16px; margin: 4px;
         cursor: pointer; font-weight: 600; }}
button:hover, button:focus {{ background: {UD_GOLD}; color: {UD_BLUE}; }}
table {{ width: 100%; border-collapse: collapse; background: white;
        border-radius: 12px; overflow: hidden;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08); }}
th {{ background: {UD_BLUE}; color: white; text-align: left;
     padding: 10px; }}
td {{ padding: 10px; border-bottom: 1px solid #e3e8ef; }}
select {{ padding: 8px; border-radius: 8px; border: 1px solid {UD_BLUE};
         margin: 4px 0 12px; display: block; }}
""")
start_server(load_bus_data(State([], [], [], "", "", False, "", 0.0)))
