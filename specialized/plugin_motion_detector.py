from plugins.base import PluginProcessBase, Process
from plugins.decorators import register
from plugins.processes_host import find_plugin, active_plugins, active_process
from Pyro4 import expose as pyro_expose, oneway as pyro_oneway
import logging
from misc.logging import ensure_logging_setup, camel_to_snake
from misc.settings import SETTINGS
from specialized.plugin_picamera import PiCameraProcessBase
from math import log, exp
from specialized.detector_support.imaging import get_denoised_motion_vector_norm, overlay_motion_vector_to_image
from specialized.detector_support.ramp import make_rgb_lut, clamp
import numpy as np
from specialized.support.thread_host import CallbackThreadHost, CallbackQueueThreadHost
from tempfile import NamedTemporaryFile
from specialized.plugin_media_manager import MEDIA_MANAGER_PLUGIN_NAME
import os


MOTION_DETECTOR_PLUGIN_NAME = 'MotionDetector'
ensure_logging_setup()
_log = logging.getLogger(camel_to_snake(MOTION_DETECTOR_PLUGIN_NAME))


MOTION_COLOR_RAMP = list(make_rgb_lut([
    (0.00, (255, 255, 255)),
    (0.25,  (66, 134, 244)),
    (0.75, (193,  65, 244)),
    (1.00, (255,   0, 246))
]))


class MotionDetectorResponder:
    @property
    def root_motion_detector_plugin(self):
        return find_plugin(MOTION_DETECTOR_PLUGIN_NAME).camera

    def _motion_status_changed_internal(self, is_moving):
        pass

    @pyro_expose
    @pyro_oneway
    def motion_status_changed(self, is_moving):
        self._motion_status_changed_internal(is_moving)


class MotionDetectorDispatcherPlugin(PluginProcessBase):
    @classmethod
    def plugin_name(cls):
        return MOTION_DETECTOR_PLUGIN_NAME

    @classmethod
    def process(cls):  # pragma: no cover
        # This plugin can run on any process
        return active_process()

    def __init__(self):
        self._notify_thread = CallbackThreadHost('notify_movement_thread', self._notify_movement)

    def __enter__(self):
        self._notify_thread.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._notify_thread.__exit__(exc_type, exc_val, exc_tb)

    def _notify_movement(self):
        value = find_plugin(self, Process.CAMERA).triggered
        proc = active_process()
        for plugin_name, plugin in active_plugins().items():
            if plugin[proc] is None or not isinstance(plugin[proc], MotionDetectorResponder):
                continue
            # noinspection PyBroadException
            try:
                plugin[proc].motion_status_changed(value)
            except:  # pragma: no cover
                _log.exception('Plugin %s has triggered an exception during motion_status_changed.', plugin_name)

    @pyro_oneway
    @pyro_expose
    def notify_movement_status_changed(self):
        self._notify_thread.wake()


class MotionDetectorCameraPlugin(MotionDetectorDispatcherPlugin, PiCameraProcessBase):
    @classmethod
    def plugin_name(cls):
        return MOTION_DETECTOR_PLUGIN_NAME

    @classmethod
    def process(cls):  # pragma: no cover
        # This plugin can run only on camera
        return Process.CAMERA

    def __init__(self):
        MotionDetectorDispatcherPlugin.__init__(self)
        PiCameraProcessBase.__init__(self)
        self._trigger_thresholds = None
        self._trigger_area_fractions = None
        self._time_window = None
        self._accumulator = None
        self._triggered = False
        self._cached_video_frame = None
        self._capture_thread = CallbackQueueThreadHost('capture_motion_image_thread', self._take_motion_image_with_info)

        def _sanitizer_tpl_of(typ, default):
            def _sanitizer(value):
                # noinspection PyBroadException
                try:
                    return tuple(typ(v) for v in value)
                except:
                    return default
            return _sanitizer

        # Load settings' defaults
        self.trigger_thresholds = SETTINGS.detector.get('trigger_thresholds',
                                                        sanitizer=_sanitizer_tpl_of(int, (80, 20)))
        self.trigger_area_fractions = SETTINGS.detector.get('trigger_area_fractions',
                                                            sanitizer=_sanitizer_tpl_of(float, (0.0001, 0.00002)))
        self.time_window = SETTINGS.detector.get('time_window', cast_to_type=float, default=2.0, ge=1.0)
        self._jpeg_quality = int(100 * SETTINGS.camera.get('jpeg_quality', cast_to_type=float, default=0.5, ge=0.0,
                                                           le=1.0))

    def __enter__(self):
        self._capture_thread.__enter__()
        MotionDetectorDispatcherPlugin.__enter__(self)
        PiCameraProcessBase.__enter__(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        PiCameraProcessBase.__exit__(self, exc_type, exc_val, exc_tb)
        MotionDetectorDispatcherPlugin.__exit__(self, exc_type, exc_val, exc_tb)
        self._capture_thread.__exit__(exc_type, exc_val, exc_tb)

    @pyro_expose
    @property
    def trigger_thresholds(self):
        return self._trigger_thresholds

    @pyro_expose
    @property
    def trigger_area_fractions(self):
        return self._trigger_area_fractions

    @pyro_expose
    @property
    def time_window(self):
        return self._time_window

    @pyro_expose
    @property
    def triggered(self):
        return self._triggered

    @pyro_expose
    @property
    def motion_estimate(self):
        return self._accumulator

    @pyro_expose
    @trigger_thresholds.setter
    def trigger_thresholds(self, value):
        self._trigger_thresholds = tuple(map(lambda x: min(max(int(x), 0), 255), value))[:2]
        assert len(self.trigger_thresholds) == 2

    @pyro_expose
    @trigger_area_fractions.setter
    def trigger_area_fractions(self, value):
        self._trigger_area_fractions = tuple(map(lambda x: min(max(float(x), 0.0), 1.0), value))[:2]
        assert len(self.trigger_thresholds) == 2

    @pyro_expose
    @time_window.setter
    def time_window(self, time):
        self._time_window = min(max(float(time), 0.01), 10000.)

    @property
    def _decay_factor(self):
        # Magic number that makes after n steps 256 decay exponentially below 1
        return exp(-8 * log(2) / (self.time_window * self.root_picamera_plugin.camera.framerate))

    @property
    def _resolution(self):
        return self.root_picamera_plugin.camera.resolution

    @property
    def _frame_area(self):
        w, h = self._resolution
        return w * h

    def _prepare_video_frame_cache(self):
        if self._cached_video_frame is None:
            self._cached_video_frame = np.empty((self._resolution[1], self._resolution[0], 3), dtype=np.uint8)

    def _take_motion_image_with_info(self, info):
        self._prepare_video_frame_cache()
        with NamedTemporaryFile(delete=False, dir=SETTINGS.get('temp_folder', cast_to_type=str, allow_none=True)) as \
                temp_file:
            media_path = temp_file.name
            _log.info('Taking motion image with info %s to %s.', str(info), media_path)
            self.root_picamera_plugin.camera.capture(self._cached_video_frame, format='rgb', use_video_port=True)
            image = overlay_motion_vector_to_image(self._cached_video_frame, self._accumulator, MOTION_COLOR_RAMP)
            image.save(temp_file, format='jpeg', quality=self._jpeg_quality)
            temp_file.flush()
            temp_file.close()
        media_mgr = find_plugin(MEDIA_MANAGER_PLUGIN_NAME, Process.CAMERA)
        if media_mgr is None:
            _log.error('Could not find a media manager on the CAMERA thread.')
            _log.warning('Discarding motion image with info %s at %s', str(info), media_path)
            try:
                os.remove(media_path)
            except OSError:  # pragma: no cover
                _log.exception('Could not delete %s.', media_path)
        else:
            media = media_mgr.deliver_media(media_path, 'jpeg', info)
            _log.info('Dispatched motion image %s with info %s at %s.', str(media.uuid), str(media.info), media.path)

    @pyro_expose
    def take_motion_picture(self, info=None):
        _log.info('Requested motion image with info %s', str(info))
        self._capture_thread.push_operation(info)

    def _updated_trigger_status(self):
        threshold = self.trigger_thresholds[1 if self.triggered else 0]
        min_area = self.trigger_area_fractions[1 if self.triggered else 0] * self._frame_area
        movement_amount_above_thresholds = (np.sum(self._accumulator > threshold) >= min_area)
        if movement_amount_above_thresholds != self.triggered:
            self._triggered = movement_amount_above_thresholds
            # Trigger all plugins
            for plugin_instance in find_plugin(self).nonempty_values():
                plugin_instance.notify_movement_status_changed()

    def analyze(self, array):  # pragma: no cover
        array = get_denoised_motion_vector_norm(array)
        if self._accumulator is None:
            self._accumulator = array
        else:
            self._accumulator *= self._decay_factor
            self._accumulator += array
        self._updated_trigger_status()


# Have a motion detector dispatcher on all procs
register(MotionDetectorDispatcherPlugin, MotionDetectorDispatcherPlugin.plugin_name(), Process.MAIN)
register(MotionDetectorDispatcherPlugin, MotionDetectorDispatcherPlugin.plugin_name(), Process.TELEGRAM)
register(MotionDetectorCameraPlugin, MotionDetectorCameraPlugin.plugin_name(), Process.CAMERA)
