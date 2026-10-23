from threading import Lock

import cv2


_configuration_lock = Lock()
_configured = False


def configure_vision_cpu_budget() -> None:
    global _configured
    with _configuration_lock:
        if not _configured:
            # Emulator workers already provide parallelism. Nested native pools
            # must not fan each image operation out over every CPU again.
            cv2.setNumThreads(1)
            _configured = True
