"""Background services started after the application runtime becomes ready."""

import threading
from concurrent.futures import Future, InvalidStateError
from threading import Event

from ok import Logger, get_path_relative_to_exe, og

logger = Logger.get_logger(__name__)

_SOUND_CONFIG_NAME = "Sound Trigger Config"
_ENABLE_SOUND_TRIGGER_KEY = "Enable Sound Trigger"


def _sound_trigger_enabled() -> bool:
    """Return whether the sound trigger global config is enabled.

    Falls back to True when the config is not readable yet, so early startup
    or missing config keeps the historic behavior.
    """
    global_config = getattr(og, "global_config", None)
    if global_config is None:
        return True
    try:
        values = global_config.get_config(_SOUND_CONFIG_NAME)
        return bool(values.get(_ENABLE_SOUND_TRIGGER_KEY, True))
    except Exception:
        return True


class RuntimeServices:
    """Own startup and shutdown for the sound and OpenVINO runtime services."""

    def __init__(self) -> None:
        self._stop_event = Event()
        self._started = False
        self._openvino_model_async = None
        self._openvino_model_future = Future()

    def start(self) -> None:
        if self._started or self._stop_event.is_set():
            return
        self._started = True
        threading.Thread(
            target=self._init_sound_context,
            daemon=True,
            name="SoundContextInit",
        ).start()
        threading.Thread(
            target=self._init_openvino,
            daemon=True,
            name="OpenVINOInit",
        ).start()

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self._openvino_model_future.cancel()
        threading.Thread(
            target=self._shutdown_sound_context,
            daemon=True,
            name="SoundContextShutdown",
        ).start()

    @property
    def openvino_model_async(self):
        if self._openvino_model_async is not None:
            return self._openvino_model_async
        return self._openvino_model_future.result()

    @property
    def openvino_latency_async(self):
        return self._openvino_model_async.latency

    @property
    def openvino_latest_image(self):
        return self._openvino_model_async.latest_image if self._openvino_model_async else None

    @property
    def openvino_available(self):
        return self.openvino_model_async._openvino_available

    def openvino_detect(
        self, image, sync=False, box=None, threshold=0.5, force=False, mask_regions=None
    ):
        if not sync:
            return self.openvino_model_async.detect(
                image,
                box=box,
                threshold=threshold,
                label="target",
                force=force,
                mask_regions=mask_regions,
            )
        return self.openvino_model_async.detect_sync(
            image,
            box=box,
            threshold=threshold,
            label="target",
            mask_regions=mask_regions,
        )

    def openvino_clear_cache(self) -> None:
        self.openvino_model_async.clear_cache()

    def _init_sound_context(self) -> None:
        from src.sound_trigger.SoundCombatContext import SoundCombatContext

        context = SoundCombatContext()
        if self._stop_event.is_set():
            return
        context.setup(
            task=None,
            sample_path=get_path_relative_to_exe("assets", "sounds", "dodge.wav"),
            counter_attack_sample_path=get_path_relative_to_exe("assets", "sounds", "counter.wav"),
        )
        if self._stop_event.is_set():
            context.shutdown()
            return
        if not _sound_trigger_enabled():
            # Keep the context set up so update_config() can lazily start the
            # listener later, but skip the WASAPI capture thread entirely.
            logger.info("Sound trigger disabled by config; audio listener not started")
            return
        if context.enter() and not self._stop_event.is_set():
            logger.info("SoundCombatContext initialized globally")
        else:
            context.shutdown()

    def _init_openvino(self) -> None:
        if self._stop_event.is_set():
            return
        try:
            logger.info("openvino_model_async Using YOLO26OpenVINOAsyncDetector")
            from src.vision.openvino_detector import YOLO26OpenVINOAsyncDetector

            detector = YOLO26OpenVINOAsyncDetector(
                xml_path=get_path_relative_to_exe("assets", "openvino", "best.xml")
            )
        except BaseException as error:
            self._publish_openvino_failure(error)
        else:
            self._publish_openvino_detector(detector)

    def _shutdown_sound_context(self) -> None:
        try:
            from src.sound_trigger.SoundCombatContext import SoundCombatContext

            SoundCombatContext().shutdown()
        except Exception:
            logger.exception("Failed to shut down SoundCombatContext")

    def _publish_openvino_failure(self, error: BaseException) -> None:
        if self._stop_event.is_set():
            return
        try:
            self._openvino_model_future.set_exception(error)
        except InvalidStateError:
            return
        logger.error(f"OpenVINO detector initialization failed: {error}")

    def _publish_openvino_detector(self, detector) -> None:
        if self._stop_event.is_set():
            return
        try:
            self._openvino_model_future.set_result(detector)
        except InvalidStateError:
            return
        self._openvino_model_async = detector
