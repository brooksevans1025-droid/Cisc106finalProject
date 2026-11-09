from drafter import * # type: ignore
from dataclasses import dataclass

#see documentation for drafter here: https://drafter-edu.github.io/drafter/
#see documentation for folium here: https://python-visualization.github.io/folium/latest/
@dataclass
class State:
    zoom_level: int


@route
def index(state: State) -> Page:
    map_of_UD = folium.Map(location=[39.6837, -75.7497], zoom_start = state.zoom_level)
    map_html = map_of_UD._repr_html_()
    return Page(state, [
        Header("UD Bus Tracker")])


@route
def add_click(state: State) -> Page:
    state.clicks = state.clicks + 1
    return index(state)


start_server(State(zoom_level = 13))
