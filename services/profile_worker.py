from multiprocessing import Event, Process, Queue
from functools import partial

from models.execution_context import ExecutionContext
from core.emulator_manager import EmulatorManager
from services.function_runner import FunctionRunner
from services.function_service import FunctionService
from services.library_service import LibraryService
from services.opencv_service import OpenCVService
from services.ocr_service import OcrService
from services.profile_service import ProfileService
from services.screen_service import ScreenService
from services.scheduler_service import SchedulerService
from services.variable_service import VariableService


class ProfileWorker:
    """Owns all function executions for one profile."""

    def __init__(
        self,
        profile: dict,
        function_service=None,
        runner_factory=FunctionRunner,
    ):

        self.profile = profile
        self.function_root = str(
            function_service.root
            if function_service
            else "data/functions"
        )
        self.runner_factory = runner_factory
        self.events = Queue()
        self.stop_event = Event()
        self.process = None

    @property
    def profile_id(self):

        return self.profile.get(
            "botstudio_profile_id",
            self.profile.get("id", ""),
        )

    def start(
        self,
        function_id: str,
        block_id: str | None = None,
        debug_values: dict | None = None,
    ):

        if self.process and self.process.is_alive():
            return False

        self.stop_event.clear()
        self.process = Process(
            target=self._run,
            args=(function_id, block_id, dict(debug_values or {})),
            name=f"BotStudioProfile-{self.profile_id}",
            daemon=True,
        )
        self.process.start()
        return True

    def stop(
        self,
    ):

        self.stop_event.set()

        if self.process and self.process.is_alive():
            self.process.join(timeout=3)

        if self.process and self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=1)

    def poll_events(
        self,
    ) -> list:

        result = []

        while not self.events.empty():
            result.append(self.events.get())

        return result

    def _run(
        self,
        function_id: str,
        block_id: str | None = None,
        debug_values: dict | None = None,
    ):

        function_service = FunctionService(
            root=self.function_root
        )

        function = function_service.load(
            self.profile.get("game", ""),
            function_id,
        )

        if not function:
            self.events.put({
                "event": "function_failed",
                "profile_id": self.profile_id,
                "function_id": function_id,
                "error": "function_not_found",
            })
            return

        context = ExecutionContext(
            game_id=self.profile.get("game", ""),
            profile_id=self.profile_id,
            function_id=function.function_id,
            variable_service=VariableService(),
            transient_variable_names=set(debug_values or {}),
            event_due_handler=partial(
                SchedulerService().set_event_due,
                self.profile.get("game", ""), self.profile_id,
            ),
            readonly_variables=ProfileService().build_runtime_globals(
                self.profile
            ),
        )
        context.variables.update(debug_values or {})

        self.events.put({
            "event": "function_started",
            "execution_id": context.execution_id,
            "profile_id": self.profile_id,
            "function_id": function.function_id,
        })

        emulator_manager = EmulatorManager()

        profile = dict(self.profile)
        profile["device_id"] = emulator_manager.get_device_id(
            profile.get("emulator_instance")
        )

        runner = self.runner_factory(
            screen_service=ScreenService(),
            opencv_service=OpenCVService(),
            adb_service=emulator_manager.adb_service,
            ocr_service=OcrService(),
            library_service=LibraryService(),
            function_service=function_service,
        )

        # live block events for the debug screen (variables snapshot included)
        from services.debug_pacing_service import DebugEventHub, DebugPacingService

        def _block_observer(block, ctx):
            DebugEventHub.instance().publish({
                "event": "block_started",
                "execution_id": ctx.execution_id,
                "profile_id": self.profile_id,
                "function_id": ctx.function_id,
                "block_id": block.block_id,
                "block_type": block.block_type,
                "variables": ctx.snapshot(),
            })

        if DebugPacingService.instance().enabled:
            runner.block_observer = _block_observer
            DebugEventHub.instance().publish({
                "event": "function_started",
                "execution_id": context.execution_id,
                "profile_id": self.profile_id,
                "function_id": function.function_id,
            })

        if self.stop_event.is_set():
            context.cancelled = True

        result = runner.run(
            function,
            context,
            profile,
            selected_block_id=block_id,
        )

        result["event"] = "function_finished"
        self.events.put(result)