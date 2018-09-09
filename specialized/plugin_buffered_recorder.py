from plugins.base import Process
from specialized.plugin_picamera import PiCameraProcessBase
from plugins.decorators import make_plugin
from specialized.camera_support.mux import DualBufferedMP4
from specialized.plugin_media_manager import MEDIA_MANAGER_PLUGIN_NAME
from plugins.processes_host import find_plugin
from Pyro4 import expose as pyro_expose
import logging
from misc.logging import ensure_logging_setup
from datetime import datetime
from misc.settings import SETTINGS
from safe_picamera import PiVideoFrameType


BUFFERED_RECORDER_PLUGIN_NAME = 'BufferedRecorder'
ensure_logging_setup()
_log = logging.getLogger(BUFFERED_RECORDER_PLUGIN_NAME.lower())


@make_plugin(BUFFERED_RECORDER_PLUGIN_NAME, Process.CAMERA)
class BufferedRecorderPlugin(PiCameraProcessBase):
    def __init__(self):
        super(BufferedRecorderPlugin, self).__init__()
        self._last_sps_header_stamp = 0
        self._recorder = DualBufferedMP4()
        self._record_user_info = None
        self._is_recording = False
        self._keep_media = True

    def __enter__(self):
        self._recorder.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._recorder.__exit__(exc_type, exc_val, exc_tb)

    @property
    def _camera(self):
        return self.picamera_root_plugin.camera

    @property
    def _last_frame(self):
        return self._camera.frame

    @property
    def _buffer_max_age(self):
        return 2 * self._camera.framerate * max(1., SETTINGS.camera.buffer)

    @property
    def _sps_header_max_age(self):
        return self._camera.framerate * max(1., SETTINGS.camera.clip_length_tolerance)

    @property
    def _last_sps_header_age(self):
        return self._recorder.total_age - self._last_sps_header_stamp

    def _handle_split_point(self):
        if self._recorder.is_recording and not self._is_recording:
            # We requested stop, but we haven't reached a split point. Now we can really stop.
            if self._keep_media:
                media_mgr = find_plugin(MEDIA_MANAGER_PLUGIN_NAME, Process.CAMERA)
                if not media_mgr:
                    _log.error('No media manager is running on the CAMERA process. Media will be dropped.')
                    self._recorder.stop_and_discard()
                else:
                    file_name = self._recorder.stop_and_finalize(self._camera.framerate, self._camera.resolution)
                    media = media_mgr.deliver_media(file_name, 'mp4', self._record_user_info)
                    _log.info('Media %s was delivered. User info: %s.', str(media.uuid), str(self._record_user_info))
            else:
                _log.info('Discarding media. User info: %s.', str(self._record_user_info))
                self._recorder.stop_and_discard()
            self._record_user_info = None
        if self._recorder.buffer_age > self._buffer_max_age:
            self._recorder.rewind_buffer()
        # Update the sps header age
        self._last_sps_header_stamp = self._recorder.total_age

    @pyro_expose
    def record(self, info=None):
        self._keep_media = True
        self._is_recording = True
        self._record_user_info = info
        self._recorder.record()

    @pyro_expose
    @property
    def is_recording(self):
        return self._recorder.is_recording and self._is_recording

    @pyro_expose
    @property
    def is_finalizing(self):
        return self._recorder.is_recording and self._keep_media and not self._is_recording

    @pyro_expose
    def stop_and_discard(self):
        self._is_recording = False
        self._keep_media = False

    @pyro_expose
    def stop_and_finalize(self):
        self._is_recording = False
        self._keep_media = True

    def write(self, data):
        # Update annotation
        self._camera.annotate_text = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # If it's a split point, one can stop
        if self._last_frame.frame_type == PiVideoFrameType.sps_header:
            self._handle_split_point()
            self._recorder.append(data, True, self._last_frame.complete)
        else:
            self._recorder.append(data, False, self._last_frame.complete)
        # Do we need to request a new sps_header
        if self._last_sps_header_age > self._sps_header_max_age:
            self._camera.request_key_frame()
