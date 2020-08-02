import asyncio
import datetime
import json
from dataclasses import dataclass, field
from itertools import islice
from operator import attrgetter
from typing import Dict, List, cast, Iterator

from asyncio import Lock

from tarpn.app import Application
from tarpn.ax25 import AX25Call, L3Protocol, AX25Packet, UIFrame, parse_ax25_call, SupervisoryCommand
from tarpn.ax25.datalink import DataLink
from tarpn.ax25.statemachine import AX25StateEvent
from tarpn.frame import L3Handler
from tarpn.netrom import NetRom, NetRomPacket, parse_netrom_packet
from tarpn.netrom.statemachine import NetRomStateMachine
from tarpn.settings import NetworkConfig
from tarpn.util import chunks


@dataclass
class Neighbor:
    call: AX25Call
    port: int
    quality: int


@dataclass
class Route:
    dest: AX25Call
    next_hop: AX25Call
    quality: int
    obsolescence: int


@dataclass
class Destination:
    node_call: AX25Call
    node_alias: str
    neighbor_map: Dict[AX25Call, Route] = field(default_factory=dict)

    def sorted_neighbors(self):
        return sorted(self.neighbor_map.values(), key=attrgetter("quality"), reverse=True)


@dataclass
class NodeDestination:
    dest_node: AX25Call
    dest_alias: str
    best_neighbor: AX25Call
    quality: int

    def __post_init__(self):
        self.quality = int(self.quality)


@dataclass
class NetRomNodes:
    sending_alias: str
    destinations: List[NodeDestination]

    def to_packets(self, source: AX25Call) -> Iterator[UIFrame]:
        for dest_chunk in chunks(self.destinations, 11):
            nodes_chunk = NetRomNodes(self.sending_alias, dest_chunk)
            yield UIFrame.ui_frame(AX25Call("NODES", 0), source, [], SupervisoryCommand.Command,
                                   False, L3Protocol.NetRom, encode_netrom_nodes(nodes_chunk))

    def save(self, source: AX25Call, file: str):
        nodes_json = {
            "nodeCall": str(source),
            "nodeAlias": self.sending_alias,
            "createdAt": datetime.datetime.now().isoformat(),
            "destinations": [{
                "nodeCall": str(d.dest_node),
                "nodeAlias": d.dest_alias,
                "bestNeighbor": str(d.best_neighbor),
                "quality": d.quality
            } for d in self.destinations]
        }
        with open(file, "w") as fp:
            json.dump(nodes_json, fp, indent=2)

    @classmethod
    def load(cls, source: AX25Call, file: str):
        with open(file) as fp:
            nodes_json = json.load(fp)
            assert str(source) == nodes_json["nodeCall"]
            sending_alias = nodes_json["nodeAlias"]
            destinations = []
            for dest_json in nodes_json["destinations"]:
                destinations.append(NodeDestination(
                    AX25Call.parse(dest_json["nodeCall"]),
                    dest_json["nodeAlias"],
                    AX25Call.parse(dest_json["bestNeighbor"]),
                    int(dest_json["quality"])
                ))
            return cls(sending_alias, destinations)


@dataclass
class RoutingTable:
    node_alias: str
    our_calls: List[AX25Call] = field(default_factory=list)
    neighbors: Dict[AX25Call, Neighbor] = field(default_factory=dict)
    destinations: Dict[AX25Call, Destination] = field(default_factory=dict)
    # TODO config all these
    default_obs: int = 100
    default_quality: int = 255
    min_quality: int = 50
    min_obs: int = 4

    def __repr__(self):
        s = "Neighbors:\n"
        for neighbor in self.neighbors.values():
            s += f"\t{neighbor}\n"
        s += "Destinations:\n"
        for dest in self.destinations.values():
            s += f"\t{dest}\n"
        return s.strip()

    def route(self, packet: NetRomPacket) -> List[AX25Call]:
        dest = self.destinations.get(packet.dest)
        if dest:
            return [n.next_hop for n in dest.sorted_neighbors()]
        else:
            if packet.dest in self.neighbors:
                return [packet.dest]
            else:
                return []

    def update_routes(self, heard_from: AX25Call, heard_on_port: int, nodes: NetRomNodes):
        """
        Update the routing table with a NODES broadcast.

        This method is not thread-safe.
        """
        # Get or create the neighbor and destination
        neighbor = self.neighbors.get(heard_from, Neighbor(heard_from, heard_on_port, self.default_quality))
        self.neighbors[heard_from] = neighbor

        # Add direct route to whoever sent the NODES
        dest = self.destinations.get(heard_from, Destination(heard_from, nodes.sending_alias))
        dest.neighbor_map[heard_from] = Route(heard_from, heard_from, self.default_quality, self.default_obs)
        self.destinations[heard_from] = dest

        for destination in nodes.destinations:
            # Filter out ourselves
            route_quality = 0
            if destination.best_neighbor in self.our_calls:
                # Best neighbor is us, this is a "trivial loop", quality is zero
                route_quality = 0
            else:
                # Otherwise compute this route's quality based on the NET/ROM spec
                route_quality = (destination.quality * neighbor.quality + 128.) / 256.

            # Only add routes which are above the minimum quality to begin with
            if route_quality > self.min_quality:
                new_dest = self.destinations.get(
                    destination.dest_node, Destination(destination.dest_node, destination.dest_alias))
                new_route = new_dest.neighbor_map.get(
                    neighbor.call, Route(destination.dest_node, destination.best_neighbor, route_quality, self.default_obs))
                new_route.quality = route_quality
                new_route.obsolescence = self.default_obs
                new_dest.neighbor_map[neighbor.call] = new_route
                self.destinations[destination.dest_node] = new_dest
            else:
                print(f"Saw new route for {neighbor.call}, but quality was too low")

    def prune_routes(self) -> None:
        """
        Prune any routes which we haven't heard about in a while.

        This method is not thread-safe.
        """
        print("Pruning routes")
        for call, destination in list(self.destinations.items()):
            for neighbor, route in list(destination.neighbor_map.items()):
                route.obsolescence -= 1
                if route.obsolescence <= 0:
                    print(f"Removing {neighbor} from {destination} neighbor's list")
                    del destination.neighbor_map[neighbor]
            if len(destination.neighbor_map.keys()) == 0:
                print(f"No more routes to {call}, removing from routing table")
                del self.destinations[call]
                if call in self.neighbors:
                    del self.neighbors[call]

    def get_nodes(self) -> NetRomNodes:
        node_destinations = []
        for destination in self.destinations.values():
            best_neighbor = None
            for neighbor in destination.sorted_neighbors():
                if neighbor.obsolescence >= self.min_obs:
                    best_neighbor = neighbor
                    break
                else:
                    print(f"Not including {neighbor} in NODES, obsolescence below threshold")
            if best_neighbor:
                node_destinations.append(NodeDestination(destination.node_call, destination.node_alias,
                                                         best_neighbor.next_hop, best_neighbor.quality))
            else:
                print(f"No good neighbor was found for {destination}")
        return NetRomNodes(self.node_alias, node_destinations)


def parse_netrom_nodes(data: bytes) -> NetRomNodes:
    bytes_iter = iter(data)
    assert next(bytes_iter) == 0xff
    sending_alias = bytes(islice(bytes_iter, 6)).decode("ASCII").strip()
    destinations = []
    while True:
        try:
            dest = parse_ax25_call(bytes_iter)
            alias = bytes(islice(bytes_iter, 6)).decode("ASCII").strip()
            neighbor = parse_ax25_call(bytes_iter)
            quality = next(bytes_iter)
            destinations.append(NodeDestination(dest, alias, neighbor, quality))
        except StopIteration:
            break
    return NetRomNodes(sending_alias, destinations)


def encode_netrom_nodes(nodes: NetRomNodes) -> bytes:
    b = bytearray()
    b.append(0xff)
    b.extend(nodes.sending_alias.ljust(6, " ").encode("ASCII"))
    for dest in nodes.destinations:
        dest.dest_node.write(b)
        b.extend(dest.dest_alias.ljust(6, " ").encode("ASCII"))
        dest.best_neighbor.write(b)
        b.append(dest.quality & 0xff)
    return bytes(b)


class NetRomNetwork(NetRom, L3Handler):
    def __init__(self, config: NetworkConfig):
        self.config = config
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sm = NetRomStateMachine(self)
        self.router = RoutingTable(config.node_alias())
        self.l3_apps: Dict[AX25Call, Application] = {}
        self.data_links: Dict[int, DataLink] = {}
        self.route_lock = Lock()
        asyncio.get_event_loop().create_task(self._broadcast_nodes())

    def maybe_handle_special(self, packet: AX25Packet) -> bool:
        if type(packet) == UIFrame:
            ui = cast(UIFrame, packet)
            if ui.protocol == L3Protocol.NetRom and ui.dest == AX25Call("NODES"):
                # Parse this NODES packet and mark it as handled
                nodes = parse_netrom_nodes(ui.info)
                asyncio.get_event_loop().create_task(self._update_nodes(packet.source, nodes))
                # Stop further processing
                return False
        return True

    def handle(self, port: int, remote_call: AX25Call, data: bytes):
        netrom_packet = parse_netrom_packet(data)
        print(f"NET/ROM: {netrom_packet}")

        # If packet is for us, handle it, otherwise route it
        if netrom_packet.dest == AX25Call.parse(self.config.node_call()):
            self.sm.handle_packet(netrom_packet)
        else:
            self.write_packet(netrom_packet)

    def nl_data(self, remote_call: AX25Call, local_call: AX25Call, protocol: L3Protocol, data: bytes):
        print(f"NL got data: {str(data)}")

    def nl_connect(self, remote_call: AX25Call, local_call: AX25Call):
        print(f"NL got connected to: {remote_call}")

    def nl_disconnect(self, remote_call: AX25Call, local_call: AX25Call):
        print(f"NL got disconnected from: {remote_call}")

    def write_packet(self, packet: NetRomPacket) -> bool:
        possible_routes = self.router.route(packet)

        routed = False
        for route in possible_routes:
            neighbor = self.router.neighbors.get(route)
            data_link = self.data_links.get(neighbor.port)
            try:
                event = AX25StateEvent.dl_data(neighbor.call, L3Protocol.NetRom, packet.buffer)
                data_link.state_machine.handle_internal_event(event)
                print(f"Routed {packet}")
                routed = True
                break
            except Exception as e:
                print(f"Had an error {e}")
                pass

        if not routed:
            print(f"Could not route packet to {packet.dest}. Possible routes were {possible_routes}")

        return routed

    def bind_data_link(self, port: int, data_link: DataLink):
        self.data_links[port] = data_link
        self.router.our_calls.append(data_link.link_call)

    async def _update_nodes(self, heard_from: AX25Call, nodes: NetRomNodes):
        async with self.route_lock:
            print(f"Got Nodes\n{nodes}")
            self.router.update_routes(heard_from, 0, nodes)
            print(f"New routing table\n{self.router}")

    async def _broadcast_nodes(self):
        await asyncio.sleep(10)  # initial delay
        while True:
            async with self.route_lock:
                self.router.prune_routes()
            nodes = self.router.get_nodes()
            nodes.save(AX25Call.parse(self.config.node_call()), "nodes.json")
            for dl in self.data_links.values():
                for nodes_packet in nodes.to_packets(AX25Call.parse(self.config.node_call())):
                    dl.write_packet(nodes_packet)
                    await asyncio.sleep(0.030)
            await asyncio.sleep(self.config.nodes_interval())
