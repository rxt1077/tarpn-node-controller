import json
import os
import queue
from dataclasses import dataclass, field, asdict
from enum import IntEnum
from typing import List, Optional, Tuple

from tarpn.datalink import L2Payload
from tarpn.transport import L4Protocol


class QoS(IntEnum):
    Highest = 0
    Higher = 1
    Default = 2
    Lower = 3
    Lowest = 4


@dataclass(order=False)
class L3Address:
    pass


@dataclass(order=True)
class L3Payload:
    source: L3Address = field(compare=False)  # TODO remove source and dest? should be encoded in the data
    destination: L3Address = field(compare=False)
    protocol: int = field(compare=False)
    buffer: bytes = field(compare=False)
    link_id: int = field(compare=False)
    qos: QoS = field(compare=True, default=QoS.Default)
    reliable: bool = field(compare=False, default=True)


class L3Queueing:
    def offer(self, packet: L3Payload) -> bool:
        raise NotImplementedError

    def maybe_take(self) -> Optional[L3Payload]:
        raise NotImplementedError


class L3PriorityQueue(L3Queueing):
    def __init__(self, max_size=20):
        self._queue = queue.PriorityQueue(max_size)

    def offer(self, packet: L3Payload):
        try:
            self._queue.put(packet, False, None)
            return True
        except queue.Full:
            return False

    def maybe_take(self) -> Optional[L3Payload]:
        try:
            return self._queue.get(True, 1.0)
        except queue.Empty:
            return None


class L3Protocol:
    def can_handle(self, protocol: int) -> bool:
        raise NotImplementedError

    def register_transport_protocol(self, protocol: L4Protocol) -> None:
        raise NotImplementedError

    def handle_l2_payload(self, payload: L2Payload):
        raise NotImplementedError

    def route_packet(self, address: L3Address) -> Tuple[bool, int]:
        raise NotImplementedError

    def send_packet(self, payload: L3Payload) -> bool:
        raise NotImplementedError

    def listen(self, address: L3Address):
        raise NotImplementedError


class L3Protocols:
    def __init__(self):
        self.protocols: List[L3Protocol] = []

    def register(self, protocol: L3Protocol):
        self.protocols.append(protocol)

    def handle_l2(self, payload: L2Payload):
        handled = False
        for protocol in self.protocols:
            # Pass the payload to any registered protocol that can handle it
            if protocol.can_handle(payload.l3_protocol):
                protocol.handle_l2_payload(payload)
                handled = True

        if not handled:
            print(f"No handler registered for protocol {payload.l3_protocol}, dropping packet")


class L3RoutingTable:
    def route1(self, destination: L3Address) -> Optional[int]:
        raise NotImplementedError
