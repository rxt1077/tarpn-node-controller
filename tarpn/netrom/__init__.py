import datetime
import json
import os
from dataclasses import dataclass
from enum import IntFlag
from itertools import islice
from typing import List, Callable, Iterator

from asyncio import Protocol

from tarpn.ax25 import AX25Call, parse_ax25_call, UIFrame, SupervisoryCommand, L3Protocol
from tarpn.util import chunks


class OpType(IntFlag):
    Unknown = 0x00
    ConnectRequest = 0x01
    ConnectAcknowledge = 0x02
    DisconnectRequest = 0x03,
    DisconnectAcknowledge = 0x04
    Information = 0x05
    InformationAcknowledge = 0x06

    def as_op_byte(self, choke: bool, nak: bool, more_follows: bool) -> int:
        """Encode the flags with the opcode into the op byte"""
        return self | (int(choke) << 7) | (int(nak) << 6) | (int(more_follows) << 5)

    def __repr__(self):
        return {
            OpType.Unknown: "???????",
            OpType.ConnectRequest: "ConnReq",
            OpType.ConnectAcknowledge: "ConnAck",
            OpType.DisconnectRequest: "DiscReq",
            OpType.DisconnectAcknowledge: "DiscAck",
            OpType.Information: "InfoReq",
            OpType.InformationAcknowledge: "InfoAck"
        }.get(self)

    @classmethod
    def create(cls, op_byte: int):
        masked = op_byte & 0x0F
        if masked in OpType.__members__.values():
            return cls(masked)
        else:
            return OpType.Unknown


@dataclass
class NetRomPacket:
    dest: AX25Call
    source: AX25Call
    ttl: int
    circuit_idx: int
    circuit_id: int
    tx_seq_num: int
    rx_seq_num: int
    op_byte: int

    @property
    def buffer(self) -> bytes:
        b = bytearray()
        self.source.write(b)
        self.dest.write(b)
        b.append(self.ttl)
        b.append(self.circuit_idx)
        b.append(self.circuit_id)
        b.append(self.tx_seq_num)
        b.append(self.rx_seq_num)
        b.append(self.op_byte)
        return bytes(b)

    def __repr__(self):
        out = f"{repr(self.op_type())} {self.source}>{self.dest} C={self.circuit_id} RX={self.rx_seq_num} TX={self.tx_seq_num} TTL={self.ttl}"
        if self.choke():
            out += " CHOKE"
        if self.nak():
            out += " NAK"
        if self.more_follows():
            out += " MORE"
        return out

    def op_type(self):
        return OpType.create(self.op_byte)

    def choke(self):
        return (self.op_byte & 0x80) == 0x80

    def nak(self):
        return (self.op_byte & 0x40) == 0x40

    def more_follows(self):
        return (self.op_byte & 0x20) == 0x20

    @classmethod
    def dummy(cls, dest: AX25Call, origin: AX25Call):
        return NetRomPacket(dest, origin, 0, 0, 0, 0, 0, 0)


@dataclass
class NetRomConnectRequest(NetRomPacket):
    proposed_window_size: int
    origin_user: AX25Call
    origin_node: AX25Call

    def __repr__(self):
        out = f"{repr(self.op_type())} {self.source}>{self.dest} C={self.circuit_id} RX={self.rx_seq_num} TX={self.tx_seq_num} TTL={self.ttl}"
        if self.choke():
            out += " CHOKE"
        if self.nak():
            out += " NAK"
        if self.more_follows():
            out += " MORE"
        return out

    @property
    def buffer(self) -> bytes:
        b = bytearray()
        self.source.write(b)
        self.dest.write(b)
        b.append(self.ttl)
        b.append(self.circuit_idx)
        b.append(self.circuit_id)
        b.append(self.tx_seq_num)
        b.append(self.rx_seq_num)
        b.append(self.op_byte)
        b.append(self.proposed_window_size)
        self.origin_user.write(b)
        self.origin_node.write(b)
        return bytes(b)


@dataclass
class NetRomConnectAck(NetRomPacket):
    accept_window_size: int

    def __repr__(self):
        out = f"{repr(self.op_type())} {self.source}>{self.dest} C={self.circuit_id} RX={self.rx_seq_num} TX={self.tx_seq_num} TTL={self.ttl}"
        if self.choke():
            out += " CHOKE"
        if self.nak():
            out += " NAK"
        if self.more_follows():
            out += " MORE"
        return out

    @property
    def buffer(self) -> bytes:
        b = bytearray()
        self.source.write(b)
        self.dest.write(b)
        b.append(self.ttl)
        b.append(self.circuit_idx)
        b.append(self.circuit_id)
        b.append(self.tx_seq_num)
        b.append(self.rx_seq_num)
        b.append(self.op_byte)
        b.append(self.accept_window_size)
        return bytes(b)


@dataclass
class NetRomInfo(NetRomPacket):
    info: bytes

    def __repr__(self):
        out = f"{repr(self.op_type())} {self.source}>{self.dest} C={self.circuit_id} RX={self.rx_seq_num} TX={self.tx_seq_num} TTL={self.ttl} {repr(self.info)}"
        if self.choke():
            out += " CHOKE"
        if self.nak():
            out += " NAK"
        if self.more_follows():
            out += " MORE"
        return out

    @property
    def buffer(self) -> bytes:
        b = bytearray()
        self.source.write(b)
        self.dest.write(b)
        b.append(self.ttl)
        b.append(self.circuit_idx)
        b.append(self.circuit_id)
        b.append(self.tx_seq_num)
        b.append(self.rx_seq_num)
        b.append(self.op_byte)
        b.extend(self.info)
        return bytes(b)


def parse_netrom_packet(data: bytes) -> NetRomPacket:
    bytes_iter = iter(data)
    origin = parse_ax25_call(bytes_iter)
    dest = parse_ax25_call(bytes_iter)

    ttl = next(bytes_iter)
    circuit_idx = next(bytes_iter)
    circuit_id = next(bytes_iter)
    tx_seq_num = next(bytes_iter)
    rx_seq_num = next(bytes_iter)
    op_byte = next(bytes_iter)
    op_type = OpType.create(op_byte)

    if op_type == OpType.ConnectRequest:
        proposed_window_size = next(bytes_iter)
        origin_user = parse_ax25_call(bytes_iter)
        origin_node = parse_ax25_call(bytes_iter)
        return NetRomConnectRequest(dest, origin, ttl, circuit_idx, circuit_id, tx_seq_num, rx_seq_num, op_byte,
                                    proposed_window_size, origin_user, origin_node)
    elif op_type == OpType.ConnectAcknowledge:
        accept_window_size = next(bytes_iter)
        return NetRomConnectAck(dest, origin, ttl, circuit_idx, circuit_id, tx_seq_num, rx_seq_num, op_byte,
                                accept_window_size)
    elif op_type == OpType.Information:
        info = bytes(bytes_iter)
        return NetRomInfo(dest, origin, ttl, circuit_idx, circuit_id, tx_seq_num, rx_seq_num, op_byte, info)
    elif op_type in (OpType.InformationAcknowledge, OpType.DisconnectRequest, OpType.DisconnectAcknowledge):
        return NetRomPacket(dest, origin, ttl, circuit_idx, circuit_id, tx_seq_num, rx_seq_num, op_byte)


class NetRom:
    def local_call(self) -> AX25Call:
        raise NotImplementedError

    def get_circuit_ids(self) -> List[int]:
        raise NotImplementedError

    def get_circuit(self, circuit_id: int):
        raise NotImplementedError

    def nl_data_request(self, my_circuit_id: int, remote_call: AX25Call, local_call: AX25Call, data: bytes) -> None:
        raise NotImplementedError

    def nl_data_indication(self, my_circuit_idx: int, my_circuit_id: int,
                           remote_call: AX25Call, local_call: AX25Call, data: bytes) -> None:
        raise NotImplementedError

    def nl_connect_request(self, remote_call: AX25Call, local_call: AX25Call,
                           origin_node: AX25Call, origin_user: AX25Call) -> None:
        raise NotImplementedError

    def nl_connect_indication(self, my_circuit_idx: int, my_circuit_id: int,
                              remote_call: AX25Call, local_call: AX25Call,
                              origin_node: AX25Call, origin_user: AX25Call) -> None:
        raise NotImplementedError

    def nl_disconnect_request(self, my_circuit_id: int, remote_call: AX25Call, local_call: AX25Call) -> None:
        raise NotImplementedError

    def nl_disconnect_indication(self, my_circuit_idx: int, my_circuit_id: int,
                                 remote_call: AX25Call, local_call: AX25Call) -> None:
        raise NotImplementedError

    def write_packet(self, packet: NetRomPacket) -> bool:
        raise NotImplementedError

    def open(self, protocol_factory: Callable[[], Protocol], local_call: AX25Call, remote_call: AX25Call,
             origin_node: AX25Call, origin_user: AX25Call) -> Protocol:
        raise NotImplementedError


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

    def to_chunks(self) -> Iterator[bytes]:
        for dest_chunk in chunks(self.destinations, 5):
            nodes_chunk = NetRomNodes(self.sending_alias, dest_chunk)
            yield encode_netrom_nodes(nodes_chunk)

    def save(self, file: str):
        nodes_json = {
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
    def load(cls, file: str):
        if not os.path.exists(file):
            return None, 0
        with open(file) as fp:
            nodes_json = json.load(fp)
            sending_alias = nodes_json["nodeAlias"]
            destinations = []
            for dest_json in nodes_json["destinations"]:
                destinations.append(NodeDestination(
                    AX25Call.parse(dest_json["nodeCall"]),
                    dest_json["nodeAlias"],
                    AX25Call.parse(dest_json["bestNeighbor"]),
                    int(dest_json["quality"])
                ))
            return cls(sending_alias, destinations), datetime.datetime.fromisoformat(nodes_json["createdAt"])


def parse_netrom_nodes(data: bytes) -> NetRomNodes:
    bytes_iter = iter(data)
    assert next(bytes_iter) == 0xff
    sending_alias = bytes(islice(bytes_iter, 6)).decode("ASCII", "replace").strip()
    destinations = []
    while True:
        try:
            dest = parse_ax25_call(bytes_iter)
            alias = bytes(islice(bytes_iter, 6)).decode("ASCII", "replace").strip()
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

