import itertools
import threading
from time import sleep
from typing import Callable, Dict, Optional

from tarpn.datalink import FrameData, L2Queuing, L2Address
from tarpn.log import LoggingMixin
from tarpn.network import L3Payload, L3Queueing
from tarpn.scheduler import Scheduler, CloseableThreadLoop
from tarpn.util import BackoffGenerator


class L2Protocol:
    def get_device_id(self) -> int:
        """
        Return a unique ID for the device this protocol is attached to
        """
        raise NotImplementedError

    def get_link_address(self) -> L2Address:
        """
        Return the address for this device
        """
        raise NotImplementedError

    def get_peer_address(self, link_id) -> L2Address:
        """
        Return the address for a peer connected to this device
        """
        raise NotImplementedError

    def peer_connected(self, link_id) -> bool:
        raise NotImplementedError

    def receive_frame(self, frame: FrameData) -> None:
        """
        Handle incoming frame data from a device.

        :param frame:
        :return:
        """
        raise NotImplementedError

    def handle_queue_full(self) -> None:
        """
        Callback for when the inbound queue is full.
        """
        raise NotImplementedError

    def maximum_transmission_unit(self) -> int:
        """
        Tell L3 how large of a payload is acceptable
        :return:
        """
        raise NotImplementedError

    def maximum_frame_size(self) -> int:
        """
        Tell L3 how large of a payload is acceptable
        :return:
        """
        raise NotImplementedError

    def send_packet(self, packet: L3Payload) -> bool:
        """
        Accept an L3 PDU, wrap it with L2 headers, and enqueue it for transmission

        :param packet:
        :return: true if the payload was accepted
        """
        raise NotImplementedError


class LinkMultiplexer:
    def __init__(self, queue_factory: Callable[[], L3Queueing], scheduler: Scheduler):
        self.queue_factory = queue_factory
        self.link_id_counter = itertools.count()

        # key is device id
        self.l2_devices: Dict[int, L2Protocol] = dict()
        self.queues: Dict[int, L3Queueing] = dict()

        # key is link id
        self.logical_links: Dict[int, L2Protocol] = dict()
        self.lock = threading.Lock()
        self.scheduler = scheduler

    def get_link(self, link_id: int) -> Optional[L2Protocol]:
        return self.logical_links.get(link_id)

    def get_queue(self, link_id: int) -> Optional[L3Queueing]:
        l2 = self.logical_links.get(link_id)
        if l2 is not None:
            return self.queues.get(l2.get_device_id())
        else:
            return None

    def register_device(self, l2_protocol: L2Protocol):
        with self.lock:
            if l2_protocol not in self.queues.keys():
                queue = self.queue_factory()
                self.queues[l2_protocol.get_device_id()] = queue
                self.l2_devices[l2_protocol.get_device_id()] = l2_protocol
                self.scheduler.submit(L2L3Driver(queue, l2_protocol))

    def add_link(self, l2_protocol: L2Protocol) -> int:
        with self.lock:
            link_id = next(self.link_id_counter)
            self.logical_links[link_id] = l2_protocol
            return link_id

    def remove_link(self, link_id: int) -> None:
        with self.lock:
            del self.logical_links[link_id]


class L2L3Driver(CloseableThreadLoop, LoggingMixin):
    def __init__(self, queue: L3Queueing, l2: L2Protocol):
        CloseableThreadLoop.__init__(self, name=f"L3-to-L2 for Port={l2.get_device_id()}")
        LoggingMixin.__init__(self)
        self.queue = queue
        self.l2 = l2
        self.retry_backoff = BackoffGenerator(0.500, 1.5, 3.000)

    def iter_loop(self):
        payload = self.queue.maybe_take()
        if payload is not None:
            sent = self.l2.send_packet(payload)
            while not sent and self.retry_backoff.total() < 20.000:
                sleep(next(self.retry_backoff))
                self.debug(f"Retrying send_packet {payload} to {self.l2}")
            if not sent:
                self.warning(f"Failed send_packet {payload} to {self.l2}")
            self.retry_backoff.reset()


class L2IOLoop(CloseableThreadLoop):
    def __init__(self, l2_queue: L2Queuing, l2_protocol: L2Protocol):
        super().__init__(name=f"IO-to-L2 for Port={l2_protocol.get_device_id()}")
        self.l2_queue = l2_queue
        self.l2_protocol = l2_protocol

    def iter_loop(self):
        frame, dropped = self.l2_queue.take_inbound()

        if dropped > 0:
            self.l2_protocol.handle_queue_full()

        if frame is not None:
            self.l2_protocol.receive_frame(frame)

    def close(self):
        self.l2_queue.close()
        super().close()
