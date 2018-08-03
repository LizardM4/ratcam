import os
import logging
import pickle
from Pyro4 import expose as pyro_expose, errors as pyro_errors, Daemon as PyroDaemon, Proxy as PyroProxy
from reboot.plugins.comm import create_sync_pair
from multiprocessing import Process


_log = logging.getLogger('singleton_host')


class SingletonHost:
    class _SingletonServer:
        @pyro_expose
        def register(self, pickled_singleton):
            singleton_cls = pickle.loads(pickled_singleton)
            uri = self._daemon.register(singleton_cls(), singleton_cls.__name__)
            _log.debug('%s: serving at %s', self._name, uri)
            return str(uri)

        @pyro_expose
        def close(self):
            _log.debug('%s: stopping', self._name)
            self._daemon.close()

        def __init__(self, daemon, name=None):
            self._daemon = daemon
            self._name = self.__class__.__name__ if name is None else name

    @staticmethod
    def _server(socket, transmit_sync, name='SingletonServer'):
        if os.path.exists(socket):
            os.remove(socket)
        daemon = PyroDaemon(unixsocket=socket)
        uri = daemon.register(SingletonHost._SingletonServer(daemon, name=name), name)
        _log.debug('%s: serving at %s', name, uri)
        transmit_sync.transmit(str(uri))
        daemon.requestLoop()
        _log.debug('%s: stopped serving at %s', name, uri)

    def __enter__(self):
        receiver, transmitter = create_sync_pair()
        self._process = Process(target=SingletonHost._server, args=(self._socket, transmitter, self._name + 'Server'),
                                name=self._name)
        self._process.start()
        _log.debug('%s: waiting for server', self._name)
        uri = receiver.receive()
        self._instance = PyroProxy(uri)
        # Default serpent serializer does not trasmit a class
        self._instance._pyroSerializer = 'marshal'
        _log.debug('%s: obtained proxy at %s', self._name, uri)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._instance.close()
        except pyro_errors.ConnectionClosedError:
            # Is this even an error?
            pass

        _log.debug('%s: signalled exit, waiting for server', self._name)
        self._process.join()
        _log.debug('%s: server joined', self._name)
        self._instance = None
        self._process = None
        if os.path.exists(self._socket):
            os.remove(self._socket)

    def __call__(self, singleton_cls):
        if self._instance is None:
            raise RuntimeError('You must __enter__ into a %s.' % self.__class__.__name__)
        # We cannot send a custom class except through Pickle, but we can't use pickle in Pyro because it's unsafe.
        # So we pickle the object and send it over marshal (because serpent does not serialize correctly bytes too)
        assert self._instance._pyroSerializer == 'marshal'
        return PyroProxy(self._instance.register(pickle.dumps(singleton_cls)))

    def __init__(self, socket, name=None):
        self._socket = socket
        self._process = None
        self._instance = None
        self._name = self.__class__.__name__ if name is None else name
