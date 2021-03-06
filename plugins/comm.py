from multiprocessing import Event, Pipe
import pickle


class SyncBase:
    def __init__(self, pipe, receive_event, trasmit_event):
        self._received = receive_event
        self._transmitted = trasmit_event
        self._pipe = pipe


class TransmitSync(SyncBase):
    def transmit(self, obj, timeout=None):
        self._pipe.send(pickle.dumps(obj))
        self._transmitted.set()
        if self._received.wait(timeout=timeout):
            self._received.clear()


class ReceiveSync(SyncBase):
    def receive(self, timeout=None):
        if self._transmitted.wait(timeout=timeout):
            data = pickle.loads(self._pipe.recv())
            self._received.set()
            self._transmitted.clear()
            return data
        return None  # pragma: no cover


def create_sync_pair(receive_cls=ReceiveSync, transmit_cls=TransmitSync):
    received_event = Event()
    transmitted_event = Event()
    receive_pipe, transmit_pipe = Pipe(False)
    return receive_cls(receive_pipe, received_event, transmitted_event), \
        transmit_cls(transmit_pipe, received_event, transmitted_event)
