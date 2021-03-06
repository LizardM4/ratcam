from Pyro4 import expose as pyro_expose, oneway as pyro_oneway
from plugins.base import PluginProcessBase, Process, ProcessPack
from plugins.decorators import register
from collections import namedtuple
from plugins.processes_host import active_process, find_plugin, active_plugins
import logging
from uuid import uuid4
from threading import Lock
import os
from misc.logging import camel_to_snake
from specialized.support.thread_host import CallbackQueueThreadHost


MEDIA_MANAGER_PLUGIN_NAME = 'MediaManager'
_log = logging.getLogger(camel_to_snake(MEDIA_MANAGER_PLUGIN_NAME))


class Media(namedtuple('_Media', ['uuid', 'owning_process', 'kind', 'path', 'info'])):
    pass


class MediaReceiver:
    def handle_media(self, media):  # pragma: no cover
        raise NotImplementedError()


class MediaManagerPlugin(PluginProcessBase):
    @classmethod
    def plugin_name(cls):
        return MEDIA_MANAGER_PLUGIN_NAME

    @classmethod
    def process(cls):  # pragma: no cover
        # This plugin can run on any process
        return active_process()

    @pyro_expose
    @pyro_oneway
    def dispatch_media(self, media):
        self._dispatch_and_delete_thread.push_operation(media)

    @pyro_expose
    @pyro_oneway
    def consume_media(self, media, process):
        with self._media_lock:
            if media.uuid not in self._media_in_use:
                return
            _log.debug('Media %s at %s was consumed by %s.', str(media.uuid), os.path.abspath(media.path),
                       process.value)
            self._media_in_use[media.uuid][process] = False
            if not any(self._media_in_use[media.uuid].values()):
                _log.debug('Media %s at %s is ready for deletion.', str(media.uuid), os.path.abspath(media.path))
                # Mark for deletion
                self._dispatch_and_delete_thread.wake()

    def deliver_media(self, path, kind, info=None):
        media_mgr_pack = find_plugin(self)
        with self._media_lock:
            uuid = None
            while uuid is None or uuid in self._media:
                uuid = uuid4()
            media = Media(uuid, active_process(), kind, path, info)
            self._media[uuid] = media
            # Assume not necessarily we have a media manager on every single process. This makes easier testing.
            self._media_in_use[uuid] = ProcessPack(*[entry is not None for entry in media_mgr_pack.values()])
            _log.info('Dispatching media %s at path %s.', str(media.uuid), os.path.abspath(media.path))
        # Dispatch to all the other media managers.
        for media_mgr in media_mgr_pack.nonempty_values():
            media_mgr.dispatch_media(media)
        return media

    def _pop_media_to_delete(self):
        with self._media_lock:
            media_to_delete = []
            for uuid, media_in_use in self._media_in_use.items():
                if not any(media_in_use.values()):
                    media_to_delete.append(self._media[uuid])
            for media in media_to_delete:
                del self._media[media.uuid]
                del self._media_in_use[media.uuid]
            return media_to_delete

    @staticmethod
    def active_local_media_receivers():
        for plugin_pack in active_plugins():
            plugin = plugin_pack[active_process()]
            if plugin is None or not isinstance(plugin, MediaReceiver):
                continue
            yield plugin

    def _dispatch_media_locally(self, media):
        for media_receiver in MediaManagerPlugin.active_local_media_receivers():
            media_receiver.handle_media(media)
        owning_manager = find_plugin(self, media.owning_process)
        if owning_manager is None:
            _log.warning('Could not consume media %s at %s, no media manager on process %s', str(media.uuid),
                         media.path, media.owning_process.value.upper())
        else:
            owning_manager.consume_media(media, active_process())

    def _delete_media_locally(self):
        for media in self._pop_media_to_delete():
            try:
                os.remove(media.path)
                _log.info('Removed media %s at %s', str(media.uuid), media.path)
            except OSError:  # pragma: no cover
                _log.exception('Could not remove %s.', os.path.abspath(media.path))

    def __enter__(self):
        self._media.clear()
        self._media_in_use.clear()
        self._dispatch_and_delete_thread.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._dispatch_and_delete_thread.__exit__(exc_type, exc_val, exc_tb)

    def __init__(self):
        self._media = {}
        self._media_in_use = {}
        self._media_lock = Lock()
        self._dispatch_and_delete_thread = CallbackQueueThreadHost(
            'media_dispatch_thread', self._dispatch_media_locally, self._delete_media_locally)


# Have a media manager on all procs
register(MediaManagerPlugin, MediaManagerPlugin.plugin_name(), Process.MAIN)
register(MediaManagerPlugin, MediaManagerPlugin.plugin_name(), Process.TELEGRAM)
register(MediaManagerPlugin, MediaManagerPlugin.plugin_name(), Process.CAMERA)
