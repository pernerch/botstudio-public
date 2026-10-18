import operator
import re
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from math import isfinite

from pathlib import Path

import cv2

from models.bot_function import BotFunction
from models.execution_context import ExecutionContext
from core.emulator import Emulator
from services.function_block_catalog import (
    BLOCK_ANDROID_BACK,
    BLOCK_CATALOG,
    DEFAULT_SWIPE_SETTLE_MS,
    BLOCK_PRESS_ENTER,
    BLOCK_CALL_FUNCTION,
    BLOCK_CONFIGURE_FIELD,
    BLOCK_COORDINATE_ANCHOR,
    BLOCK_COMPARE,
    BLOCK_CONDITION,
    BLOCK_DURATION_PARSE,
    BLOCK_DUE_DATE_REMAINING,
    BLOCK_FAILURE,
    BLOCK_FIND_AND_TAP_ELEMENT,
    BLOCK_FIND_COLOR_TRIGGER,
    BLOCK_FIND_ELEMENT,
    BLOCK_FIND_SHAPE,
    BLOCK_GATE,
    BLOCK_MATH,
    BLOCK_NEGATE_OUTCOME,
    BLOCK_OCR_READ,
    BLOCK_REPEAT,
    BLOCK_SUCCESS,
    BLOCK_SPLITTER,
    BLOCK_STOP_EVENT,
    BLOCK_SET_EVENT_DUE,
    BLOCK_RESCHEDULE_EVENT,
    BLOCK_SWIPE,
    BLOCK_SWIPE_SEARCH,
    BLOCK_SWIPE_SEARCH_TAP,
    BLOCK_TIMER,
    BLOCK_SEARCH_OCR,
    BLOCK_SEARCH_OCR_TAP,
    BLOCK_TAP_ELEMENT,
    BLOCK_TAP_POINT,
    BLOCK_CLEAR_FIELD,
    BLOCK_TYPE_TEXT,
    BLOCK_WAIT,
    BLOCK_WAIT_FOR_ELEMENT,
    BLOCK_WAIT_FOR_ELEMENT_AND_TAP,
    BLOCK_VARIABLE,
    OUTCOME_BODY,
    OUTCOME_COORDINATES,
    OUTCOME_DONE,
    OUTCOME_ELAPSED,
    OUTCOME_FAILED,
    OUTCOME_FALSE,
    OUTCOME_FOUND,
    OUTCOME_NOT_FOUND,
    OUTCOME_OK,
    OUTCOME_OUT1,
    OUTCOME_OUT2,
    OUTCOME_OUT3,
    OUTCOME_OUT4,
    OUTCOME_PENDING,
    OUTCOME_SUCCESS,
    OUTCOME_TRUE,
)
from services.logging_service import get_logger
from services.execution_activity_service import get_execution_activity
from services.coordinate_search_service import build_offset_search_region, resolve_ocr_region


# "52d 12:41:59", "12:41:59" or a plain seconds number.
_DURATION_PATTERN = re.compile(
    r"^(?:(?P<days>\d+)\s*d\s+)?(?P<hours>\d{1,3}):(?P<minutes>\d{2}):(?P<seconds>\d{2})$"
)

# Every block that declares a coordinates outcome must hand its match to wired targets.
COORDINATE_SOURCE_BLOCKS = frozenset(
    block_type
    for block_type, spec in BLOCK_CATALOG.items()
    if OUTCOME_COORDINATES in spec.get("outcomes", ())
)


class FunctionRunner:
    """Executes one function inside one isolated ExecutionContext."""

    KEYCODE_BACK = 4
    KEYCODE_ENTER = 66

    def __init__(
        self,
        screen_service=None,
        opencv_service=None,
        adb_service=None,
        ocr_service=None,
        library_service=None,
        function_service=None,
        logger=None,
        activity_service=None,
    ):

        self.screen_service = screen_service
        self.opencv_service = opencv_service
        self.adb_service = adb_service
        self.ocr_service = ocr_service
        self.library_service = library_service
        self.function_service = function_service
        self.logger = logger or get_logger("function")
        self.activity_service = activity_service if activity_service is not None else get_execution_activity()
        self.trace = []
        self._block_failure = None
        # optional callable(block, context) fired before each block; the
        # debug screen subscribes to drive the live block highlight
        self.block_observer = None
    
    @staticmethod
    def _cancellable_sleep(context, seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            if context.is_cancelled():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.05, remaining))

    def run(
        self,
        function: BotFunction,
        context: ExecutionContext,
        profile: dict | None = None,
        selected_block_id: str | None = None,
    ) -> dict:
        activity = getattr(self, "activity_service", None)
        started_at = time.perf_counter()
        outcome = OUTCOME_FAILED
        with activity.track(function, context, profile) if activity is not None else nullcontext():
            try:
                result = self._run(function, context, profile, selected_block_id)
                outcome = result.get("outcome", OUTCOME_FAILED)
                return result
            finally:
                context.function_executions.append({
                    "function_id": function.function_id,
                    "outcome": outcome,
                    "duration_seconds": time.perf_counter() - started_at,
                })

    def _run(
        self,
        function: BotFunction,
        context: ExecutionContext,
        profile: dict | None = None,
        selected_block_id: str | None = None,
    ) -> dict:

        blocks = function.blocks

        self.trace = []
        started_at = time.perf_counter()

        self.logger.info(
            "Function Run Started",
            profile=profile.get("name") if profile else None,
            function_id=function.function_id,
            function_name=function.name,
            execution_id=context.execution_id,
            parent_id=context.parent_id,
            selected_block_id=selected_block_id or "",
        )

        if selected_block_id:
            selected = next(
                (
                    block
                    for block in function.walk_blocks()
                    if block.block_id == selected_block_id
                ),
                None,
            )
            if selected is None:
                return self._finish_run(
                    function,
                    context,
                    profile,
                    self._result(
                        context,
                        OUTCOME_FAILED,
                        "selected block not found in the saved function",
                        selected_block_id,
                    ),
                    started_at,
                )

            result = self._run_debug_block(
                function,
                selected,
                context,
                profile,
            )
            return self._finish_run(
                function,
                context,
                profile,
                result,
                started_at,
            )

        else:
            blocks = self._entry_blocks(
                function
            )

        if not blocks:
            return self._finish_run(
                function,
                context,
                profile,
                self._result(
                    context,
                    OUTCOME_FAILED,
                    "function has no blocks",
                ),
                    started_at,
            )

        return self._finish_run(
            function,
            context,
            profile,
            self._run_sequence(
                blocks,
                function,
                context,
                profile,
            ),
            started_at,
        )

    def _finish_run(
        self,
        function,
        context,
        profile,
        result,
        started_at,
    ):

        if result.get("outcome") == OUTCOME_FAILED:
            block = next(
                (item for item in function.walk_blocks()
                 if item.block_id == result.get("block_id")),
                None,
            )
            own = {"function_id": function.function_id, "function_name": function.name}
            nested = result.get("failure")
            chain = [own]
            if isinstance(nested, dict):
                chain.extend(nested.get("function_chain", []))
            result["failure"] = {
                "function_id": nested.get("function_id") if isinstance(nested, dict) else function.function_id,
                "function_name": nested.get("function_name") if isinstance(nested, dict) else function.name,
                "block_id": nested.get("block_id") if isinstance(nested, dict) else result.get("block_id"),
                "block_type": nested.get("block_type") if isinstance(nested, dict) else (
                    block.block_type if block else None
                ),
                "function_chain": chain[:8],
            }

        self.logger.info(
            "Function Run Finished",
            profile=profile.get("name") if profile else None,
            function_id=function.function_id,
            execution_id=context.execution_id,
            parent_id=context.parent_id,
            outcome=result.get("outcome"),
            block_status="{0} [{1}]".format(
                result.get("outcome"),
                self.format_trace(self.trace),
            ),
            variables=self.format_variable_accesses(context.variable_accesses),
            steps=len(self.trace),
            duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
            variable_accesses=context.variable_accesses,
        )

        return result

    def _debug_blocks(
        self,
        function: BotFunction,
        selected_block_id: str,
    ):
        selected = next(
            (
                block
                for block in function.walk_blocks()
                if block.block_id == selected_block_id
            ),
            None,
        )
        if not selected:
            return []
        return [selected]

    def _run_debug_block(self, function, selected, context, profile):
        outcome, value, error = self._run_debug_operation(
            function,
            selected,
            context,
            profile,
            visited=set(),
        )
        result_outcome = (
            OUTCOME_FAILED
            if outcome in (OUTCOME_FAILED, OUTCOME_NOT_FOUND)
            else OUTCOME_SUCCESS
        )
        result = self._result(
            context,
            result_outcome,
            value,
            selected.block_id,
        )
        if error:
            result["error"] = error
        return result

    def _run_debug_operation(self, function, block, context, profile, visited):
        if block.block_id in visited:
            return OUTCOME_FAILED, None, "coordinate source cycle detected"
        visited.add(block.block_id)

        coordinate_connection = next(
            (
                connection
                for connection in function.connections
                if connection.target_block_id == block.block_id
                and connection.target_input == OUTCOME_COORDINATES
            ),
            None,
        )
        if coordinate_connection:
            source = next(
                (
                    candidate
                    for candidate in function.walk_blocks()
                    if candidate.block_id == coordinate_connection.source_block_id
                ),
                None,
            )
            if source is None:
                return OUTCOME_FAILED, None, "coordinate source block not found"
            source_outcome, source_value, source_error = self._run_debug_operation(
                function,
                source,
                context,
                profile,
                visited,
            )
            if source_error or source_outcome in (OUTCOME_FAILED, OUTCOME_NOT_FOUND):
                return OUTCOME_FAILED, None, source_error or "coordinate source failed"
            if not isinstance(source_value, dict) or not {"x", "y"}.issubset(source_value):
                return OUTCOME_FAILED, None, "coordinate source returned no coordinates"
            context.set(
                f"__coordinates_{block.block_id}__",
                source_value,
                local=True,
            )

        outcome, value, error = self.run_block(block, context, profile)
        debug_payload = None
        stored_value = value
        # blocks may return {"value": ..., "debug": ...}: the plain value feeds
        # output_as variables (typed persistence), the debug payload stays in
        # the trace so the block status can show the full expression
        if isinstance(value, dict) and set(value.keys()) == {"value", "debug"}:
            debug_payload = value["debug"]
            stored_value = value["value"]
        self.trace.append({
            "block_id": block.block_id,
            "block_type": block.block_type,
            "outcome": outcome,
            "value": debug_payload if debug_payload is not None else stored_value,
            "error": error,
        })
        if block.output_as:
            # see _run_sequence: a rejected write must surface as a block
            # failure, not as an exception out of the debug run
            try:
                context.set(block.output_as, stored_value)
            except Exception as write_error:
                return (
                    OUTCOME_FAILED,
                    stored_value,
                    f"output variable '{block.output_as}' could not be written: {write_error}",
                )
        return outcome, stored_value, error

    def _entry_blocks(
        self,
        function: BotFunction,
    ):
        """Wired targets are reached through their branch, so only roots start a run."""

        wired = {
            connection.target_block_id
            for connection in function.connections
            if connection.target_input != "break"
        }

        if not wired:
            return function.blocks

        roots = [
            block
            for block in function.blocks
            if block.block_id not in wired
        ]

        return roots or function.blocks[:1]

    def _run_sequence(
        self,
        blocks,
        function,
        context,
        profile,
        depth=0,
        path_blocks=None,
    ):

        if depth > 64:
            return self._result(
                context,
                OUTCOME_FAILED,
                "maximum branch depth exceeded",
            )

        path_blocks = set(path_blocks or ())
        ended_result = None
        for block in blocks:

            if context.event_stop_requested:
                return self._result(
                    context,
                    OUTCOME_SUCCESS,
                    block.block_id,
                )

            if block.block_id in path_blocks:
                if block.block_type == BLOCK_REPEAT:
                    return self._result(
                        context,
                        OUTCOME_SUCCESS,
                        block.block_id,
                    )
                return self._result(
                    context,
                    OUTCOME_FAILED,
                    "function graph cycle detected",
                    block.block_id,
                )
            active_path = path_blocks | {block.block_id}

            if context.is_cancelled():
                return self._result(
                    context,
                    OUTCOME_FAILED,
                    "cancelled",
                )

            if block.block_type == BLOCK_REPEAT:
                result = self._run_repeat(
                    block,
                    function,
                    context,
                    profile,
                    depth,
                    active_path,
                )
                if result["outcome"] != OUTCOME_SUCCESS:
                    failed_branch = block.branches.get(OUTCOME_FAILED, [])
                    if result["outcome"] == OUTCOME_FAILED and failed_branch:
                        if self._branch_reaches_block(failed_branch, block.block_id):
                            self.logger.error(
                                "Repeat Failure Branch Re-enters Repeat",
                                profile=profile.get("name") if profile else None,
                                function_id=function.function_id,
                                execution_id=context.execution_id,
                                block_id=block.block_id,
                                next_action="end function instead of retrying an exhausted repeat",
                            )
                            return result
                        failed_result = self._run_sequence(
                            failed_branch,
                            function,
                            context,
                            profile,
                            depth + 1,
                            active_path,
                        )
                        if failed_result["outcome"] != OUTCOME_SUCCESS:
                            return failed_result
                        continue
                    return result
                continue

            self._block_failure = None
            outcome, value, error = self.run_block(
                block,
                context,
                profile,
            )

            debug_payload = None
            stored_value = value
            # see _run_single_block: {"value": ..., "debug": ...} keeps typed
            # variables clean while the trace shows the full expression
            if isinstance(value, dict) and set(value.keys()) == {"value", "debug"}:
                debug_payload = value["debug"]
                stored_value = value["value"]

            self.trace.append({
                "block_id": block.block_id,
                "block_type": block.block_type,
                "outcome": outcome,
                "value": debug_payload if debug_payload is not None else stored_value,
                "error": error,
                "failure": self._block_failure,
                "function_id": function.function_id,
                "function_name": function.name,
                "event_stop_requested": context.event_stop_requested,
                "reschedule_after_seconds": context.event_reschedule_seconds,
            })

            self.logger.info(
                "Function Block Executed",
                profile=profile.get("name") if profile else None,
                function_id=function.function_id,
                execution_id=context.execution_id,
                parent_id=context.parent_id,
                block_id=block.block_id,
                block_type=block.block_type,
                outcome=outcome,
                value=self._summarize_value(stored_value),
            )

            if block.output_as:
                # a rejected typed or scoped write is a data condition
                # (DEC-154) and must end the block visibly instead of
                # aborting the whole run with an exception
                try:
                    context.set(
                        block.output_as,
                        stored_value,
                    )
                except Exception as write_error:
                    reason = (
                        f"output variable '{block.output_as}' could not be "
                        f"written: {write_error}"
                    )
                    # the trace step was recorded before the write, so the
                    # reason is attached there for _result and the UI
                    self.trace[-1]["outcome"] = OUTCOME_FAILED
                    self.trace[-1]["error"] = reason
                    self.logger.error(
                        "Function Output Write Rejected",
                        profile=profile.get("name") if profile else None,
                        function_id=function.function_id,
                        execution_id=context.execution_id,
                        block_id=block.block_id,
                        variable=block.output_as,
                        error=str(write_error),
                    )
                    return self._result(
                        context,
                        OUTCOME_FAILED,
                        None,
                        block.block_id,
                    )

            if context.event_stop_requested:
                self.logger.info(
                    "Function Event Stop Requested",
                    function_id=function.function_id,
                    execution_id=context.execution_id,
                    block_id=block.block_id,
                    reschedule_after_seconds=context.event_reschedule_seconds,
                )
                return self._result(
                    context,
                    OUTCOME_SUCCESS,
                    stored_value,
                    block.block_id,
                )

            break_targets = set()
            if block.block_type == BLOCK_SPLITTER:
                executable_branch = []
                for split_outcome, targets in block.branches.items():
                    for target in targets:
                        if target not in executable_branch:
                            executable_branch.append(target)
            else:
                branch = block.branches.get(
                    outcome,
                    [],
                )

                coordinate_targets = []
                if block.block_type in COORDINATE_SOURCE_BLOCKS and outcome in (
                    OUTCOME_COORDINATES, OUTCOME_FOUND,
                ):
                    coordinate_targets = block.branches.get(
                        OUTCOME_COORDINATES,
                        [],
                    )
                    for target in coordinate_targets:
                        context.set(
                            f"__coordinates_{target.block_id}__",
                            value,
                            local=True,
                        )

                # Check for break signal connections into loop blocks
                break_targets = {
                    conn.target_block_id
                    for conn in function.connections
                    if conn.source_block_id == block.block_id
                    and conn.source_outcome == outcome
                    and conn.target_input == "break"
                }
                for target_id in break_targets:
                    context.set(f"__break_{target_id}__", True, local=True)

                executable_branch = [
                    target
                    for target in branch
                    if target.block_id not in break_targets
                ]
                for target in coordinate_targets:
                    if target not in executable_branch:
                        executable_branch.append(target)

            if error:
                # a block that could not work (error set) is only reported as an
                # error when its outcome is unwired; a wired route handles it
                # and must not raise an alert (DEC-164)
                handled = bool(executable_branch or break_targets)
                log_failure = self.logger.warning if handled else self.logger.error
                log_failure(
                    "Function Block Failure Handled" if handled else "Function Block Failed",
                    profile=profile.get("name") if profile else None,
                    function_id=function.function_id,
                    execution_id=context.execution_id,
                    parent_id=context.parent_id,
                    block_id=block.block_id,
                    block_type=block.block_type,
                    outcome=outcome,
                    error=error,
                    next_action=(
                        "handled by the wired route"
                        if handled
                        else "wire the failed outcome or inspect block parameters"
                    ),
                )

            # a negative outcome is a route, not an abort; the path ends when it is unwired
            if not executable_branch:
                self.logger.info(
                    "Function Path Ended",
                    profile=profile.get("name") if profile else None,
                    function_id=function.function_id,
                    execution_id=context.execution_id,
                    parent_id=context.parent_id,
                    block_id=block.block_id,
                    outcome=outcome,
                )
                # only a block that could not work (error set) fails the run;
                # outcome-only negatives end the path as a clean route and
                # carry the route's outcome so callers can react to it
                if error:
                    return self._result(
                        context,
                        OUTCOME_FAILED,
                        error,
                        block.block_id,
                    )
                if outcome in (OUTCOME_FAILED, OUTCOME_NOT_FOUND):
                    return self._result(
                        context,
                        OUTCOME_FAILED,
                        stored_value,
                        block.block_id,
                    )
                # a cleanly ended path must not cut off sibling targets of the
                # same outcome (multi-target wiring, splitter branches)
                ended_result = self._result(
                    context,
                    OUTCOME_SUCCESS,
                    stored_value,
                    block.block_id,
                )
                continue

            self.logger.info(
                "Function Branch Taken",
                profile=profile.get("name") if profile else None,
                function_id=function.function_id,
                execution_id=context.execution_id,
                parent_id=context.parent_id,
                from_block_id=block.block_id,
                outcome=outcome,
                to_block_ids=[target.block_id for target in executable_branch],
            )

            result = self._run_sequence(
                executable_branch,
                function,
                context,
                profile,
                depth + 1,
                active_path,
            )

            if result["outcome"] != OUTCOME_SUCCESS:
                return result
            ended_result = result

        if ended_result is not None:
            return ended_result
        return self._result(
            context,
            OUTCOME_SUCCESS,
        )

    @staticmethod
    def _branch_reaches_block(blocks, target_block_id, visited=None):
        visited = visited or set()
        for block in blocks:
            if block.block_id == target_block_id:
                return True
            if block.block_id in visited:
                continue
            visited.add(block.block_id)
            for children in block.branches.values():
                if FunctionRunner._branch_reaches_block(
                    children,
                    target_block_id,
                    visited,
                ):
                    return True
        return False

    def _run_repeat(
        self,
        block,
        function,
        context,
        profile,
        depth,
        path_blocks=None,
    ):

        maximum_raw = block.params.get("max_iterations", 1)
        if isinstance(maximum_raw, str) and maximum_raw.startswith("$"):
            maximum_raw = context.get(maximum_raw[1:])
        try:
            maximum = max(1, int(float(maximum_raw)))
        except (TypeError, ValueError):
            return self._result(
                context,
                OUTCOME_FAILED,
                f"repeat max_iterations is not a number: {maximum_raw!r}",
                block.block_id,
            )

        timeout_ms = int(
            block.params.get(
                "timeout_ms",
                0,
            )
        )

        deadline = (
            time.monotonic() + timeout_ms / 1000
            if timeout_ms > 0
            else None
        )

        body = block.branches.get(
            OUTCOME_BODY,
            [],
        )

        if not body:
            return self._result(
                context,
                OUTCOME_FAILED,
                "repeat has no body connected",
                block.block_id,
            )

        context.local_variables.pop(f"__break_{block.block_id}__", None)

        self.logger.info(
            "Function Repeat Started",
            profile=profile.get("name") if profile else None,
            function_id=function.function_id,
            execution_id=context.execution_id,
            parent_id=context.parent_id,
            block_id=block.block_id,
            max_iterations=maximum,
            timeout_ms=timeout_ms,
        )

        exited = False
        for _ in range(maximum):

            if deadline and time.monotonic() >= deadline:
                return self._result(
                    context,
                    OUTCOME_FAILED,
                    "repeat timeout reached before exit condition",
                    block.block_id,
                )

            if context.is_cancelled():
                return self._result(
                    context,
                    OUTCOME_FAILED,
                    "cancelled",
                    block.block_id,
                )

            result = self._run_sequence(
                body,
                function,
                context,
                profile,
                depth + 1,
                path_blocks,
            )

            if context.event_stop_requested:
                return self._result(
                    context,
                    OUTCOME_SUCCESS,
                    block.block_id,
                )

            if result["outcome"] == OUTCOME_FAILED:
                if block.params.get("until_failed"):
                    exited = True
                    break
                return result

            if not block.params.get("until_failed"):
                until_var = str(
                    block.params.get("until_variable", "")
                ).lstrip("$").strip()

                if (
                    until_var and context.get(until_var)
                ) or context.local_variables.get(f"__break_{block.block_id}__", False):
                    exited = True
                    break

        if not exited:
            return self._result(
                context,
                OUTCOME_FAILED,
                "repeat maximum iterations reached without exit condition",
                block.block_id,
            )

        done_branch = block.branches.get(
            OUTCOME_DONE,
            [],
        )
        if done_branch:
            return self._run_sequence(
                done_branch,
                function,
                context,
                profile,
                depth + 1,
                path_blocks,
            )

        return self._result(
            context,
            OUTCOME_SUCCESS,
            block.block_id,
        )

    def run_block(
        self,
        block,
        context,
        profile,
    ):
        from core.command_control import CommandCancelled, command_control

        if context.is_cancelled():
            return OUTCOME_FAILED, None, "cancelled"
        with command_control(context.is_cancelled):
            try:
                return self._run_block(block, context, profile)
            except CommandCancelled:
                return OUTCOME_FAILED, None, "cancelled"

    def _run_block(self, block, context, profile):
        # debug pacing: only active when Debug Mode is on, otherwise a no-op
        from services.debug_pacing_service import DebugPacingService
        if not DebugPacingService.instance().before_block(context.is_cancelled):
            return OUTCOME_FAILED, None, "cancelled"

        if self.block_observer is not None:
            try:
                self.block_observer(block, context)
            except Exception:
                # observation must never break the run
                pass

        if block.block_type == BLOCK_STOP_EVENT:
            return self._stop_event(block, context)

        if block.block_type == BLOCK_CONFIGURE_FIELD:
            return OUTCOME_OK, None, None

        params = self._resolve_params(
            block.params,
            context,
        )

        try:
            if block.block_type == BLOCK_WAIT:
                if not self._cancellable_sleep(
                    context,
                    max(0, int(params.get("duration_ms", 0))) / 1000,
                ):
                    return OUTCOME_FAILED, None, "cancelled"
                return "done", None, None

            if block.block_type == BLOCK_COORDINATE_ANCHOR:
                return self._coordinate_anchor(params)

            if block.block_type == BLOCK_TAP_POINT:
                return self._tap_point(
                    block,
                    params,
                    context,
                    profile,
                )

            if block.block_type == BLOCK_CLEAR_FIELD:
                return self._clear_field(
                    block,
                    params,
                    context,
                    profile,
                )

            if block.block_type == BLOCK_TYPE_TEXT:
                return self._type_text(
                    block,
                    params,
                    context,
                    profile,
                )

            if block.block_type == BLOCK_SWIPE:
                return self._swipe(
                    params,
                    profile,
                    context,
                )

            if block.block_type == BLOCK_SWIPE_SEARCH:
                return self._swipe_search(
                    params,
                    profile,
                    context,
                )

            if block.block_type == BLOCK_SWIPE_SEARCH_TAP:
                return self._swipe_search_tap(params, profile, context)

            if block.block_type == BLOCK_SEARCH_OCR:
                return self._search_ocr(
                    params,
                    profile,
                )

            if block.block_type == BLOCK_SEARCH_OCR_TAP:
                return self._search_ocr_tap(params, profile, context)

            if block.block_type == BLOCK_ANDROID_BACK:
                return self._android_back(
                    profile,
                )

            if block.block_type == BLOCK_PRESS_ENTER:
                return self._press_enter(
                    profile,
                    params,
                    context,
                )

            if block.block_type == BLOCK_COMPARE:
                return self._compare(
                    params,
                    context,
                )

            if block.block_type == BLOCK_DURATION_PARSE:
                return self._duration_parse(
                    params,
                    context,
                )

            if block.block_type == BLOCK_DUE_DATE_REMAINING:
                return self._due_date_remaining(
                    params,
                    context,
                )

            if block.block_type == BLOCK_MATH:
                return self._math(
                    params,
                    context,
                )

            if block.block_type == BLOCK_GATE:
                return self._gate(
                    params,
                    context,
                )

            if block.block_type == BLOCK_TIMER:
                return self._timer(
                    params,
                    context,
                )

            if block.block_type == BLOCK_CONDITION:
                return self._condition(
                    params,
                    context,
                )

            if block.block_type in (
                BLOCK_FIND_ELEMENT,
                BLOCK_FIND_COLOR_TRIGGER,
                BLOCK_FIND_SHAPE,
                BLOCK_FIND_AND_TAP_ELEMENT,
                BLOCK_TAP_ELEMENT,
                BLOCK_WAIT_FOR_ELEMENT,
                BLOCK_WAIT_FOR_ELEMENT_AND_TAP,
            ):
                return self._find_element(
                    block,
                    block.block_type,
                    params,
                    context,
                    profile,
                )

            if block.block_type == BLOCK_OCR_READ:
                return self._ocr_read(
                    block,
                    params,
                    context,
                    profile,
                )

            if block.block_type == BLOCK_REPEAT:
                return "done", None, None

            if block.block_type == BLOCK_SPLITTER:
                return OUTCOME_OK, None, None

            if block.block_type == BLOCK_SUCCESS:
                return OUTCOME_SUCCESS, None, None

            # the failure terminal is a routable end, not a system error
            if block.block_type == BLOCK_FAILURE:
                return OUTCOME_FAILED, None, None

            if block.block_type == BLOCK_CALL_FUNCTION:
                return self._call_function(
                    block,
                    params,
                    context,
                    profile,
                )

            if block.block_type == BLOCK_VARIABLE:
                return self._variable(
                    params,
                    context,
                )

            if block.block_type in (BLOCK_SET_EVENT_DUE, BLOCK_RESCHEDULE_EVENT):
                return self._set_event_due(block, context)

            if block.block_type == BLOCK_NEGATE_OUTCOME:
                return self._negate_outcome(
                    params,
                )

            return (
                OUTCOME_FAILED,
                None,
                f"block type not implemented: {block.block_type}",
            )

        except Exception as ex:
            return OUTCOME_FAILED, None, str(ex)

    def _call_function(
        self,
        block,
        params,
        context,
        profile,
    ):
        function_id = params.get("function_id")
        if not function_id:
            return OUTCOME_FAILED, None, "no helper function selected"

        if not self.function_service:
            return OUTCOME_FAILED, None, "function service unavailable"

        game_id = profile.get("game", "") if profile else ""
        helper = self.function_service.load(game_id, function_id)
        if not helper and self.function_service.root.exists():
            for g in self.function_service.root.iterdir():
                if g.is_dir():
                    helper = self.function_service.load(g.name, function_id)
                    if helper:
                        break

        if not helper:
            return OUTCOME_FAILED, None, f"helper function '{function_id}' not found"

        helper_context = ExecutionContext(
            game_id=context.game_id,
            profile_id=context.profile_id,
            function_id=helper.function_id,
            variable_service=context.variable_service,
            event_id=context.event_id,
            task_id=context.task_id,
            field_names=set(context.field_names),
            event_due_handler=context.event_due_handler,
            event_stop_allowed=context.event_stop_allowed,
            variables=dict(context.variables),
            readonly_variables=dict(context.readonly_variables),
            transient_variable_names=set(context.transient_variable_names),
            cancellation_checker=context.cancellation_checker,
            parent_id=context.execution_id,
            function_executions=context.function_executions,
        )

        inputs = params.get("inputs", {})
        if isinstance(inputs, dict):
            for k, v in inputs.items():
                resolved_v = self._resolve_params({k: v}, context).get(k)
                helper_context.set(k, resolved_v)

        caller_trace = self.trace
        caller_failure = self._block_failure
        try:
            result = self.run(
                helper,
                helper_context,
                profile,
            )
        finally:
            self.trace = caller_trace
            self._block_failure = caller_failure
        self.trace.extend(result.get("trace", []))

        context.variables.update(helper_context.variables)

        if result.get("event_stop_requested"):
            context.request_event_stop(result.get("reschedule_after_seconds"))
            return OUTCOME_OK, result.get("value"), None

        if result.get("outcome") == OUTCOME_SUCCESS:
            return OUTCOME_SUCCESS, result.get("value"), None

        if result.get("outcome") == OUTCOME_FAILED:
            self._block_failure = result.get("failure")
            self.logger.warning(
                "Helper function failed; propagating failure to caller.",
                function_id=function_id,
                execution_id=helper_context.execution_id,
                parent_id=context.execution_id,
                helper_outcome=result.get("outcome"),
                error=result.get("error"),
            )
            return (
                OUTCOME_FAILED,
                result.get("value"),
                result.get("error") or "helper function failed",
            )

        return OUTCOME_SUCCESS, result.get("value"), None

    def _variable(
        self,
        params,
        context,
    ):
        """Read or write a runtime variable through the configured mode."""

        name = str(
            params.get("variable", "")
        ).strip()
        mode = str(
            params.get("mode", "read")
        ).lower()

        if not name:
            return OUTCOME_FAILED, None, "no variable selected"

        if mode == "read":
            if not self._variable_exists(name, context):
                return OUTCOME_FAILED, None, f"variable '{name}' not found"
            return OUTCOME_OK, context.get(name), None

        if mode == "write":
            context.set(
                name,
                params.get("value"),
            )
            return OUTCOME_OK, context.get(name), None

        return OUTCOME_FAILED, None, f"unsupported variable mode: {mode}"

    @staticmethod
    def _variable_exists(name, context) -> bool:
        normalized = str(name).lstrip("$")
        if normalized in context.readonly_variables:
            return True
        if normalized.startswith("local:"):
            return normalized[6:] in context.local_variables
        if normalized.startswith("global:"):
            return normalized[7:] in context.variables
        return (
            normalized in context.local_variables
            or normalized in context.variables
        )

    def _negate_outcome(
        self,
        params,
    ):
        """Invert an explicitly supplied success or failure outcome."""

        outcome = str(
            params.get("outcome", OUTCOME_SUCCESS)
        ).lower()

        if outcome == OUTCOME_SUCCESS:
            return OUTCOME_FAILED, None, None

        if outcome == OUTCOME_FAILED:
            return OUTCOME_SUCCESS, None, None

        return OUTCOME_FAILED, None, (
            f"cannot negate unsupported outcome: {outcome}"
        )

    def _set_event_due(self, block, context):
        if context.event_due_handler is None:
            return OUTCOME_FAILED, None, "scheduler event control is unavailable in this execution context"
        target = str(block.params.get("event_id") or "").strip()
        if not target:
            return OUTCOME_FAILED, None, "no target event selected"
        delay = 0.0
        if block.block_type == BLOCK_RESCHEDULE_EVENT:
            variable = str(block.params.get("delay_variable") or "").strip()
            raw = context.get(variable) if variable else block.params.get("delay_seconds", 0)
            try:
                delay = float(raw)
            except (TypeError, ValueError):
                return OUTCOME_FAILED, None, "reschedule delay must be a number of seconds"
            if not isfinite(delay) or delay < 0:
                return OUTCOME_FAILED, None, "reschedule delay must be finite and nonnegative seconds"
        due_at = context.event_due_handler(target, delay)
        return OUTCOME_OK, due_at, None

    def _stop_event(self, block, context):
        if not context.event_stop_allowed:
            return OUTCOME_FAILED, None, "Stop event is only available in regular scheduler events"

        try:
            stop_after = int(block.params.get("stop_after", 1))
        except (TypeError, ValueError):
            return OUTCOME_FAILED, None, "stop arrival count must be a positive integer"
        if stop_after < 1:
            return OUTCOME_FAILED, None, "stop arrival count must be at least one"

        counter_name = f"__stop_event_count_{block.block_id}"
        arrival_count = int(context.local_variables.get(counter_name, 0)) + 1
        context.set(counter_name, arrival_count, local=True)
        if arrival_count < stop_after:
            return OUTCOME_OK, arrival_count, None

        variable_name = str(block.params.get("delay_variable") or "").strip()
        raw_delay = (
            context.get(variable_name)
            if variable_name
            else block.params.get("delay_seconds", 0)
        )
        try:
            delay_seconds = float(raw_delay)
        except (TypeError, ValueError):
            return OUTCOME_FAILED, None, "reschedule delay must be a number of seconds"
        if not isfinite(delay_seconds) or delay_seconds < 0:
            return OUTCOME_FAILED, None, "reschedule delay must be finite and nonnegative seconds"

        context.request_event_stop(delay_seconds)
        return OUTCOME_OK, delay_seconds, None

    def cancel(
        self,
        context: ExecutionContext,
    ) -> None:

        context.cancelled = True

    def _resolve_params(
        self,
        params,
        context,
    ):

        def resolve(value):

            if isinstance(value, str) and value.startswith("$"):
                return context.get(value[1:])

            if isinstance(value, dict):
                return {
                    key: resolve(item)
                    for key, item in value.items()
                }

            if isinstance(value, list):
                return [resolve(item) for item in value]

            return value

        return {
            key: resolve(value)
            for key, value in params.items()
        }

    @staticmethod
    def _coordinate_anchor(params):
        try:
            width = int(params.get("width", 0))
            height = int(params.get("height", 0))
            if width <= 0 or height <= 0:
                return OUTCOME_FAILED, None, "coordinate anchor needs positive width and height"
            return OUTCOME_COORDINATES, {
                "x": int(params.get("x", 0)),
                "y": int(params.get("y", 0)),
                "width": width,
                "height": height,
                "screen_width": int(params.get("screen_width", 0)),
                "screen_height": int(params.get("screen_height", 0)),
            }, None
        except (TypeError, ValueError):
            return OUTCOME_FAILED, None, "coordinate anchor contains invalid values"

    def _duration_parse(
        self,
        params,
        context,
    ):
        """Parses OCR duration text (e.g. "52d 12:41:59") into seconds.

        An unrecognized format is a data condition, not a runtime error: it
        branches on OUTCOME_FAILED instead of aborting the function run.
        """

        raw = params.get("input")
        if isinstance(raw, str) and raw.startswith("$"):
            raw = context.get(raw[1:])
        text = str(raw).strip() if raw is not None else ""

        try:
            return OUTCOME_OK, float(text), None
        except (TypeError, ValueError):
            pass

        match = _DURATION_PATTERN.match(text)
        if not match:
            return OUTCOME_FAILED, None, None

        days = int(match.group("days") or 0)
        hours = int(match.group("hours"))
        minutes = int(match.group("minutes"))
        seconds = int(match.group("seconds"))
        total_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds
        return OUTCOME_OK, float(total_seconds), None

    def _due_date_remaining(
        self,
        params,
        context,
    ):
        """Return nonnegative seconds until an ISO due date, or zero if absent/expired."""

        raw = params.get("input")
        if raw is None:
            return OUTCOME_OK, 0, None

        if isinstance(raw, datetime):
            due_date = raw
        else:
            text = str(raw).strip()
            if not text or text.casefold() == "none":
                return OUTCOME_OK, 0, None
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                due_date = datetime.fromisoformat(text)
            except ValueError:
                return (
                    OUTCOME_FAILED,
                    None,
                    f"due date is not a valid ISO date/time: {text!r}",
                )

        if due_date.tzinfo is None:
            due_date = due_date.replace(tzinfo=timezone.utc)
        remaining = (due_date - datetime.now(timezone.utc)).total_seconds()
        return OUTCOME_OK, max(0, remaining), None

    def _compare(
        self,
        params,
        context,
    ):
        """Compare two values; numeric strings are compared numerically."""

        left = params.get("left")
        right = params.get("right")
        op = str(params.get("operator", "==")).strip()

        if isinstance(left, str) and left.startswith("$"):
            left = context.get(left[1:])

        if isinstance(right, str) and right.startswith("$"):
            right = context.get(right[1:])

        def as_number(value):
            try:
                return float(str(value).strip())
            except (TypeError, ValueError):
                return None

        left_num = as_number(left)
        right_num = as_number(right)
        if left_num is not None and right_num is not None:
            left, right = left_num, right_num

        operations = {
            "==": operator.eq,
            "!=": operator.ne,
            "<": operator.lt,
            "<=": operator.le,
            ">": operator.gt,
            ">=": operator.ge,
        }

        if op not in operations:
            return OUTCOME_FAILED, None, f"unsupported operator: {op}"

        result = operations[op](left, right)
        display = f"{self._summarize_value(left)} {op} {self._summarize_value(right)}"
        return (
            OUTCOME_TRUE if result else "false",
            {
                "value": result,
                "debug": {"expression": display, "result": result},
            },
            None,
        )

    def _math(
        self,
        params,
        context,
    ):
        """Evaluate left OP right and return a numeric result."""

        left = params.get("left")
        right = params.get("right")
        op = str(params.get("operator", "+")).strip()

        if isinstance(left, str) and left.startswith("$"):
            left = context.get(left[1:])

        if isinstance(right, str) and right.startswith("$"):
            right = context.get(right[1:])

        try:
            left = float(left)
            right = float(right)
        except (TypeError, ValueError):
            return OUTCOME_FAILED, None, "math operands must be numeric"

        operations = {
            "+": operator.add,
            "-": operator.sub,
            "*": operator.mul,
            "/": operator.truediv,
        }

        if op not in operations:
            return OUTCOME_FAILED, None, f"unsupported operator: {op}"

        if op == "/" and right == 0:
            return OUTCOME_FAILED, None, "division by zero"

        result = operations[op](left, right)
        display = f"{self._summarize_value(left)} {op} {self._summarize_value(right)} = {result:g}"
        return OUTCOME_OK, {
            "value": result,
            "debug": {"expression": display, "result": result},
        }, None

    def _condition(
        self,
        params,
        context,
    ):
        """Branches on the truthiness of an already resolved value.

        `_resolve_params` turns a `$Name` reference into its value before the
        block runs, so a typed Yes/No field arrives here as a real bool. Only
        an unresolved reference still needs a lookup.
        """

        value = params.get("expression", "")
        if isinstance(value, str) and value.startswith("$"):
            value = context.get(value[1:])
        result = bool(value)
        return (
            OUTCOME_TRUE if result else OUTCOME_FALSE,
            {
                "value": result,
                "debug": {
                    "expression": self._summarize_value(value),
                    "result": result,
                },
            },
            None,
        )

    def _gate(
        self,
        params,
        context,
    ):

        values = [
            bool(context.get(value.lstrip("$")))
            if isinstance(value, str)
            else bool(value)
            for value in params.get("operands", [])
        ]
        op = params.get("operator", "and")

        if op == "and":
            result = all(values)
        elif op == "or":
            result = any(values)
        elif op == "not":
            result = not values[0] if values else True
        elif op == "nand":
            result = not all(values)
        elif op == "nor":
            result = not any(values)
        elif op == "xor":
            result = sum(values) == 1
        else:
            return OUTCOME_FAILED, None, f"unsupported gate: {op}"

        return OUTCOME_TRUE if result else "false", result, None

    def _timer(self, params, context):
        """Branch on a remaining cooldown or an absolute timestamp."""

        raw = params.get("variable", "")
        if isinstance(raw, str) and (
            raw.startswith("$")
            or raw.startswith("local:")
            or raw.startswith("global:")
        ):
            raw = context.get(raw)

        remaining = None
        if isinstance(raw, (int, float)):
            numeric = float(raw)
            remaining = numeric - datetime.now(timezone.utc).timestamp() if numeric > 1_000_000_000 else numeric
        elif raw not in (None, ""):
            text = str(raw).strip()
            try:
                numeric = float(text)
                remaining = numeric - datetime.now(timezone.utc).timestamp() if numeric > 1_000_000_000 else numeric
            except ValueError:
                try:
                    stamp = text[:-1] + "+00:00" if text.endswith("Z") else text
                    remaining = datetime.fromisoformat(stamp).timestamp() - datetime.now(timezone.utc).timestamp()
                except ValueError:
                    match = _DURATION_PATTERN.match(text)
                    if match:
                        remaining = (
                            int(match.group("days") or 0) * 86400
                            + int(match.group("hours")) * 3600
                            + int(match.group("minutes")) * 60
                            + int(match.group("seconds"))
                        )

        if remaining is None:
            return OUTCOME_PENDING, None, "timer variable has no supported value"

        margin = max(0.0, float(params.get("margin_seconds", 0) or 0))
        return (
            OUTCOME_ELAPSED if remaining <= margin else OUTCOME_PENDING,
            remaining,
            None,
        )

    def _device(self, profile):

        if not self.adb_service:
            return None, "ADB service unavailable"

        device_id = profile.get("device_id") if profile else None

        if not device_id:
            return None, "profile device unavailable"

        if not self.adb_service.ensure_connection(device_id):
            return None, f"device not connected: {device_id}"

        return device_id, None

    def _tap_point(self, block, params, context, profile):

        device_id, error = self._device(
            profile
        )

        if error:
            return OUTCOME_FAILED, None, error

        # a wired coordinates input (e.g. from Search OCR) overrides the typed X/Y
        coordinates = context.get(f"__coordinates_{block.block_id}__")
        if isinstance(coordinates, dict) and "x" in coordinates and "y" in coordinates:
            x = int(coordinates.get("x", 0))
            y = int(coordinates.get("y", 0))
        else:
            x = int(params.get("x", 0))
            y = int(params.get("y", 0))

        if not self._pre_tap_delay(params, context):
            return OUTCOME_FAILED, None, "cancelled"

        if not Emulator(device_id=device_id).tap(x, y):
            return OUTCOME_FAILED, None, "adb tap rejected"

        return OUTCOME_OK, None, None

    def _tap_target(self, block, params, context):
        """Resolve x/y from a wired coordinates input or the typed params."""

        coordinates = context.get(f"__coordinates_{block.block_id}__")
        if isinstance(coordinates, dict) and "x" in coordinates and "y" in coordinates:
            return int(coordinates.get("x", 0)), int(coordinates.get("y", 0))
        return int(params.get("x", 0)), int(params.get("y", 0))

    def _clear_field(self, block, params, context, profile):
        """Tap a field and delete its content with backspace key events."""

        device_id, error = self._device(profile)
        if error:
            return OUTCOME_FAILED, None, error

        x, y = self._tap_target(block, params, context)
        count = max(1, int(params.get("count", 32)))

        emulator = Emulator(device_id=device_id)
        if not emulator.tap(x, y):
            return OUTCOME_FAILED, None, "adb tap rejected"
        if not emulator.clear_text(count):
            return OUTCOME_FAILED, None, "adb clear rejected"
        return OUTCOME_OK, None, None

    def _type_text(self, block, params, context, profile):
        """Tap a field and type a fixed value or a $variable into it."""

        raw = params.get("text", "")
        if isinstance(raw, str) and raw.startswith("$"):
            raw = context.get(raw[1:], "")
        text = str(raw)
        if text == "":
            return OUTCOME_FAILED, None, "no text configured"

        device_id, error = self._device(profile)
        if error:
            return OUTCOME_FAILED, None, error

        x, y = self._tap_target(block, params, context)

        emulator = Emulator(device_id=device_id)
        if not emulator.tap(x, y):
            return OUTCOME_FAILED, None, "adb tap rejected"
        if not emulator.type_text(text):
            return OUTCOME_FAILED, None, "adb type text rejected"
        return OUTCOME_OK, None, None

    def _swipe(self, params, profile, context):

        device_id, error = self._device(
            profile
        )

        if error:
            return OUTCOME_FAILED, None, error

        if not self.screen_service:
            return OUTCOME_FAILED, None, "screen service unavailable"

        if profile is None:
            return OUTCOME_FAILED, None, "profile unavailable"

        screenshot, capture_error = self._capture_screen(profile)
        if capture_error:
            return OUTCOME_FAILED, None, capture_error
        image = cv2.imread(str(screenshot))
        if image is None:
            return OUTCOME_FAILED, None, "screen capture could not be read"

        height, width = image.shape[:2]
        direction = str(params.get("direction", "up")).lower()
        max_swipes = max(1, int(params.get("max_swipes", 1)))
        duration_ms = max(100, int(params.get("duration_ms", 300)))
        between_swipes_ms = max(0, int(params.get("between_swipes_ms", 200)))
        swipe_start_x, swipe_start_y, end_x, end_y = self._swipe_points(
            direction,
            int(params.get("start_x", 0)),
            int(params.get("start_y", 0)),
            width,
            height,
        )

        emulator = Emulator(device_id=device_id)
        for index in range(max_swipes):
            if index and not self._cancellable_sleep(context, between_swipes_ms / 1000):
                return OUTCOME_FAILED, None, "cancelled"
            if not emulator.swipe(
                swipe_start_x,
                swipe_start_y,
                end_x,
                end_y,
                duration_ms,
            ):
                return OUTCOME_FAILED, None, "adb swipe rejected"

        settle_ms = max(0, int(params.get("settle_ms", DEFAULT_SWIPE_SETTLE_MS)))
        if settle_ms and not self._cancellable_sleep(context, settle_ms / 1000):
            return OUTCOME_FAILED, None, "cancelled"

        return OUTCOME_OK, None, None

    def _android_back(self, profile):

        device_id, error = self._device(
            profile
        )

        if error:
            return OUTCOME_FAILED, None, error

        if not Emulator(device_id=device_id).keyevent(
            self.KEYCODE_BACK
        ):
            return OUTCOME_FAILED, None, "adb keyevent rejected"

        return OUTCOME_OK, None, None

    def _press_enter(self, profile, params=None, context=None):

        device_id, error = self._device(
            profile
        )

        if error:
            return OUTCOME_FAILED, None, error

        params = params or {}
        if not self._pre_tap_delay(
            {"tap_delay_ms": params.get("delay_ms", 500)},
            context,
        ):
            return OUTCOME_FAILED, None, "cancelled"

        if not Emulator(device_id=device_id).keyevent(
            self.KEYCODE_ENTER
        ):
            return OUTCOME_FAILED, None, "adb keyevent rejected"

        return OUTCOME_OK, None, None

    def _swipe_search(self, params, profile, context):

        template_path, error = self._template_path(
            params
        )

        if error:
            return OUTCOME_FAILED, None, error

        if not self.screen_service:
            return OUTCOME_FAILED, None, "screen service unavailable"

        if not self.opencv_service:
            return OUTCOME_FAILED, None, "detection service unavailable"

        if profile is None:
            return OUTCOME_FAILED, None, "profile unavailable"

        threshold = self._threshold(
            params
        )
        direction = str(params.get("direction", "up")).lower()
        max_swipes = max(1, int(params.get("max_swipes", 5)))
        duration_ms = max(100, int(params.get("duration_ms", 300)))
        between_swipes_ms = max(0, int(params.get("between_swipes_ms", 200)))
        start_x = int(params.get("start_x", 0))
        start_y = int(params.get("start_y", 0))

        device_id, error = self._device(profile)
        if error:
            return OUTCOME_FAILED, None, error

        for index in range(max_swipes):
            if index and not self._cancellable_sleep(context, between_swipes_ms / 1000):
                return OUTCOME_FAILED, None, "cancelled"
            screenshot, capture_error = self._capture_screen(profile)
            if capture_error:
                return OUTCOME_FAILED, None, capture_error
            detection = self.opencv_service.find_best(
                str(screenshot),
                template_path,
                threshold,
                self._library_boundary(params),
            )

            if detection.found:
                match = {
                    "x": detection.x,
                    "y": detection.y,
                    "width": detection.w,
                    "height": detection.h,
                    "score": round(detection.score, 4),
                }
                return OUTCOME_FOUND, match, None

            image = cv2.imread(str(screenshot))
            if image is None:
                return OUTCOME_NOT_FOUND, None, None

            height, width = image.shape[:2]

            try:
                swipe_start_x, swipe_start_y, end_x, end_y = self._swipe_points(
                    direction,
                    start_x,
                    start_y,
                    width,
                    height,
                )
            except ValueError:
                return OUTCOME_FAILED, None, f"unsupported swipe direction: {direction}"

            if not Emulator(device_id=device_id).swipe(
                swipe_start_x,
                swipe_start_y,
                end_x,
                end_y,
                duration_ms,
            ):
                return OUTCOME_FAILED, None, "adb swipe rejected"

            if not self._cancellable_sleep(context, duration_ms / 1000):
                return OUTCOME_FAILED, None, "cancelled"

        return OUTCOME_NOT_FOUND, None, None

    @staticmethod
    def _swipe_points(direction, start_x, start_y, width, height):

        if direction == "up":
            swipe_start_x = start_x if start_x else width // 2
            swipe_start_y = start_y if start_y else int(height * 0.8)
            return swipe_start_x, swipe_start_y, swipe_start_x, int(height * 0.2)

        if direction == "down":
            swipe_start_x = start_x if start_x else width // 2
            swipe_start_y = start_y if start_y else int(height * 0.2)
            return swipe_start_x, swipe_start_y, swipe_start_x, int(height * 0.8)

        if direction == "left":
            swipe_start_x = start_x if start_x else int(width * 0.8)
            swipe_start_y = start_y if start_y else height // 2
            return swipe_start_x, swipe_start_y, int(width * 0.2), swipe_start_y

        if direction == "right":
            swipe_start_x = start_x if start_x else int(width * 0.2)
            swipe_start_y = start_y if start_y else height // 2
            return swipe_start_x, swipe_start_y, int(width * 0.8), swipe_start_y

        raise ValueError(direction)

    def _find_element(self, block, block_type, params, context, profile):

        coordinates = context.get(f"__coordinates_{block.block_id}__")
        coordinate_region = None
        detection_profile = self._library_detection_profile(params) or {}
        detection_mode = str(detection_profile.get("mode", "template")).lower()

        template_path = None
        error = None
        if detection_mode != "color":
            template_path, error = self._template_path(params)

        if error:
            return OUTCOME_FAILED, None, error

        if not self.screen_service:
            return OUTCOME_FAILED, None, "screen service unavailable"

        if not self.opencv_service:
            return OUTCOME_FAILED, None, "detection service unavailable"

        if profile is None:
            return OUTCOME_FAILED, None, "profile unavailable"

        if isinstance(coordinates, dict) and "x" in coordinates and "y" in coordinates:
            entry = self.library_service.load(params.get("library_entry_id", "")) if self.library_service else None
            coordinate_region = build_offset_search_region(
                coordinates,
                entry or {},
                params.get("offset_x", 0),
                params.get("offset_y", 0),
            )
            if coordinate_region is None:
                match = dict(coordinates)
                if block_type in (
                    BLOCK_TAP_ELEMENT,
                    BLOCK_FIND_AND_TAP_ELEMENT,
                    BLOCK_WAIT_FOR_ELEMENT_AND_TAP,
                ):
                    return self._tap_match(match, params, profile)
                return OUTCOME_FOUND, match, None

        threshold = self._threshold(
            params
        )

        attempts, delay_ms = self._detection_schedule(
            block_type,
            params,
        )

        detection = None
        capture_error = None

        for attempt in range(attempts):

            if attempt and not self._cancellable_sleep(context, delay_ms / 1000):
                return OUTCOME_FAILED, None, "cancelled"

            screenshot, capture_error = self._capture_screen(profile)
            if capture_error:
                # one unreadable frame is retried by the next attempt
                continue

            search_region = coordinate_region or self._library_boundary(params)
            if detection_mode == "color":
                detection = self.opencv_service.find_color_best(
                    str(screenshot),
                    detection_profile,
                    search_region,
                )
            else:
                detection = self.opencv_service.find_best(
                    str(screenshot),
                    template_path,
                    threshold,
                    search_region,
                    detection_profile,
                )

            if detection.found:
                break

        if detection is None and capture_error:
            return OUTCOME_FAILED, None, capture_error

        if not detection or not detection.found:
            if block_type in (
                BLOCK_FIND_AND_TAP_ELEMENT,
                BLOCK_TAP_ELEMENT,
                BLOCK_WAIT_FOR_ELEMENT_AND_TAP,
            ):
                return OUTCOME_FAILED, None, None
            return (
                OUTCOME_NOT_FOUND,
                None,
                None,
            )

        match = {
            "x": detection.x,
            "y": detection.y,
            "width": detection.w,
            "height": detection.h,
            "score": round(detection.score, 4),
        }

        if block_type not in (
            BLOCK_TAP_ELEMENT,
            BLOCK_FIND_AND_TAP_ELEMENT,
            BLOCK_WAIT_FOR_ELEMENT_AND_TAP,
        ):
            return OUTCOME_FOUND, match, None

        tap_params = dict(params)
        if coordinate_region:
            tap_params["offset_x"] = 0
            tap_params["offset_y"] = 0

        return self._tap_detection(
            detection,
            tap_params,
            profile,
            match,
            context,
        )

    def _tap_match(self, match, params, profile, context=None):
        device_id, error = self._device(profile)
        if error:
            return OUTCOME_FAILED, None, error

        if not self._pre_tap_delay(params, context):
            return OUTCOME_FAILED, None, "cancelled"

        x = int(match.get("x", 0)) + int(match.get("width", 0)) // 2
        y = int(match.get("y", 0)) + int(match.get("height", 0)) // 2
        x += int(params.get("offset_x", 0))
        y += int(params.get("offset_y", 0))
        if not Emulator(device_id=device_id).tap(x, y):
            return OUTCOME_FAILED, None, "adb tap rejected"
        match["tap_x"] = x
        match["tap_y"] = y
        return OUTCOME_OK, match, None

    def _pre_tap_delay(self, params, context) -> bool:
        """Wait the configured pre-tap delay (default 500 ms), cancellable."""

        delay_ms = max(0, int(params.get("tap_delay_ms", 500) or 0))
        if delay_ms <= 0:
            return True
        if context is None:
            time.sleep(delay_ms / 1000)
            return True
        return self._cancellable_sleep(context, delay_ms / 1000)

    def _search_ocr(self, params, profile):
        # search_ocr only declares found/not_found/coordinates outcomes, so
        # configuration errors must resolve to not_found instead of failed.
        if not self.ocr_service:
            return OUTCOME_NOT_FOUND, None, "OCR service is not configured"
        if not self.screen_service:
            return OUTCOME_NOT_FOUND, None, "screen service unavailable"
        if profile is None:
            return OUTCOME_NOT_FOUND, None, "profile unavailable"

        query = str(params.get("query", "")).strip()
        if not query:
            return OUTCOME_NOT_FOUND, None, "no OCR search text configured"

        screenshot, capture_error = self._capture_screen(profile)
        if capture_error:
            return OUTCOME_FAILED, None, capture_error
        match = self.ocr_service.find_text(
            str(screenshot),
            query,
            params.get("region"),
        )
        if not match:
            return OUTCOME_NOT_FOUND, None, None
        return OUTCOME_FOUND, match, None

    def _capture_screen(self, profile):
        """Capture the screen; file/ADB problems become a block result."""
        try:
            return self.screen_service.capture(profile), None
        except (OSError, RuntimeError) as error:
            self.logger.warning(
                "Screen Capture Failed",
                profile=(profile or {}).get("name"),
                error=repr(error),
            )
            return None, f"screenshot capture failed: {error}"

    def _swipe_search_tap(self, params, profile, context):
        outcome, match, error = self._swipe_search(params, profile, context)
        if outcome != OUTCOME_FOUND:
            return outcome, match, error
        return self._tap_match(match, params, profile, context)

    def _search_ocr_tap(self, params, profile, context=None):
        outcome, match, error = self._search_ocr(params, profile)
        if outcome != OUTCOME_FOUND:
            return outcome, match, error
        return self._tap_match(match, params, profile, context)

    def _tap_detection(self, detection, params, profile, match, context=None):

        device_id, error = self._device(
            profile
        )

        if error:
            return OUTCOME_FAILED, None, error

        if not self._pre_tap_delay(params, context):
            return OUTCOME_FAILED, None, "cancelled"

        x = (
            detection.x
            + detection.w // 2
            + int(params.get("offset_x", 0))
        )
        y = (
            detection.y
            + detection.h // 2
            + int(params.get("offset_y", 0))
        )

        if not Emulator(device_id=device_id).tap(x, y):
            return OUTCOME_FAILED, None, "adb tap rejected"

        match["tap_x"] = x
        match["tap_y"] = y

        return OUTCOME_OK, match, None

    def _template_path(self, params):

        entry_id = params.get(
            "library_entry_id",
            "",
        )

        if not entry_id:
            return None, "no library element selected"

        if not self.library_service:
            return None, "library service unavailable"

        entry = self.library_service.load(
            entry_id
        )

        if not entry:
            return None, f"library element not found: {entry_id}"

        template_path = (
            entry.get("pattern", {})
            .get("reference_path", "")
        )

        if not template_path:
            template_path = str(
                self.library_service.root
                / entry_id
                / "pattern_reference.png"
            )

        if not Path(template_path).exists():
            return None, f"library reference image missing: {entry_id}"

        return template_path, None

    def _threshold(self, params):
        # the catalog stores percent, OpenCV expects a 0..1 ratio
        value = float(
            params.get("threshold", 98)
        )

        return value / 100 if value > 1 else value

    def _detection_schedule(self, block_type, params):

        if block_type in (BLOCK_WAIT_FOR_ELEMENT, BLOCK_WAIT_FOR_ELEMENT_AND_TAP):

            poll_ms = max(
                50,
                int(params.get("poll_ms", 500)),
            )

            timeout_ms = max(
                poll_ms,
                int(params.get("timeout_ms", 10000)),
            )

            return timeout_ms // poll_ms, poll_ms

        return (
            max(1, int(params.get("attempts", 1))),
            int(params.get("delay_ms", 300)),
        )

    def _ocr_read(self, block, params, context, profile):

        if not self.ocr_service:
            return OUTCOME_FAILED, None, "OCR service is not configured"

        if not self.screen_service:
            return OUTCOME_FAILED, None, "screen service unavailable"

        if profile is None:
            return OUTCOME_FAILED, None, "profile unavailable"

        coordinates = context.get(f"__coordinates_{block.block_id}__")
        has_coordinates = (
            isinstance(coordinates, dict)
            and "x" in coordinates
            and "y" in coordinates
            and "width" in coordinates
            and "height" in coordinates
        )
        if (
            not params.get("library_entry_id")
            and not isinstance(params.get("region"), dict)
            and not has_coordinates
        ):
            return OUTCOME_FAILED, None, "no library element selected for OCR"

        delay_ms = max(0, int(params.get("delay_before_read_ms", 200) or 0))
        if delay_ms and not self._cancellable_sleep(context, delay_ms / 1000):
            return OUTCOME_FAILED, None, "cancelled"

        try:
            screenshot = self.screen_service.capture(profile)
        except Exception as ex:
            # a corrupt or missing screenshot is a real background error
            return OUTCOME_FAILED, None, f"screenshot capture failed: {ex}"
        try:
            value = self.ocr_service.read(
                str(screenshot),
                self._ocr_boundary(block, params, context),
                params.get("expect", "text"),
                params.get("language", ""),
                counter_part=params.get("counter_part", "left"),
            )
        except Exception as ex:
            default = params.get("default_value", "")
            if str(default).strip() == "":
                return OUTCOME_FAILED, None, str(ex)
            value = self._coerce_ocr_default(
                params.get("expect", "text"),
                default,
            )

        return OUTCOME_OK, value, None

    @staticmethod
    def _coerce_ocr_default(expect, default):
        """Interpret the configured default as number or text to match the type."""

        text = str(default).strip()
        if expect in ("number", "counter"):
            try:
                number = float(text)
            except ValueError:
                return text
            return int(number) if number.is_integer() else number
        return text

    def _ocr_boundary(self, block, params, context):

        coordinates = context.get(
            f"__coordinates_{block.block_id}__",
        )
        boundary = params.get("region")
        entry = None
        if not isinstance(boundary, dict):
            entry_id = params.get("library_entry_id", "")
            if self.library_service and entry_id:
                entry = self.library_service.load(entry_id)
            boundary = entry.get("boundary") if isinstance(entry, dict) else None
        self.logger.info(
            "OCR Boundary Resolved",
            block_id=block.block_id,
            has_coordinates=isinstance(coordinates, dict),
            coordinates=coordinates if isinstance(coordinates, dict) else None,
            has_region=isinstance(params.get("region"), dict),
            region=params.get("region") if isinstance(params.get("region"), dict) else None,
            boundary=boundary,
            offset_x=params.get("offset_x", 0),
            offset_y=params.get("offset_y", 0),
        )
        result = resolve_ocr_region(
            params,
            coordinates if isinstance(coordinates, dict) else None,
            entry if isinstance(entry, dict) else None,
        )
        source = (
            "region" if isinstance(params.get("region"), dict)
            else "library" if entry
            else "coordinates" if result
            else "none"
        )
        self.logger.info("OCR Boundary Result", block_id=block.block_id, boundary=result, source=source)
        return result

    def _library_boundary(self, params):

        if not self.library_service:
            return None

        entry_id = params.get("library_entry_id", "")
        if not entry_id:
            return None

        entry = self.library_service.load(entry_id) or {}
        boundary = entry.get("boundary")
        return boundary if isinstance(boundary, dict) else None

    def _library_detection_profile(self, params):
        if not self.library_service:
            return None
        entry_id = params.get("library_entry_id", "")
        if not entry_id:
            return None
        entry = self.library_service.load(entry_id) or {}
        profile = entry.get("detection_profile")
        return profile if isinstance(profile, dict) else None

    def _result(
        self,
        context,
        outcome,
        value=None,
        block_id=None,
    ):

        # a failed run keeps the reason of the last step that reported one
        error = None
        if outcome == OUTCOME_FAILED:
            for step in reversed(self.trace):
                if step.get("error"):
                    error = step["error"]
                    break

        result = {
            "execution_id": context.execution_id,
            "profile_id": context.profile_id,
            "function_id": context.function_id,
            "block_id": block_id,
            "outcome": outcome,
            "value": value,
            "event_stop_requested": context.event_stop_requested,
            "reschedule_after_seconds": context.event_reschedule_seconds,
            "variables": context.snapshot(),
            "trace": list(self.trace),
        }
        if error:
            result["error"] = error
        if outcome == OUTCOME_FAILED:
            step = next(
                (item for item in reversed(self.trace) if item.get("block_id") == block_id),
                None,
            )
            if step and isinstance(step.get("failure"), dict):
                result["failure"] = step["failure"]
        return result

    @staticmethod
    def format_trace(trace) -> str:
        """Block-Kette wie im Block-Status des Function Editors.

        Jeder Block meldet mindestens sein Outcome (ok/failed/true/false/...).
        Blöcke mit Rückgabewert zeigen zusätzlich eine kompakte Detailansicht,
        damit sich ohne weitere Werkzeuge debuggen lässt, was gerechnet oder
        verglichen wurde.
        """

        steps = []
        for step in trace or []:
            block_type = step.get("block_type")
            status = step.get("error") or step.get("outcome")
            if not status:
                status = "no outcome"
            if step.get("event_stop_requested"):
                delay = step.get("reschedule_after_seconds")
                status = f"event stop: {delay:g}s" if delay is not None else "event stop"

            value = step.get("value")
            detail = None
            if block_type in ("compare", "math") and isinstance(value, dict):
                detail = value.get("expression")
                result = value.get("result")
                if detail is not None and result is not None and block_type == "compare":
                    detail = f"{detail} → {result}"
            elif block_type == "ocr_read" and value is not None:
                detail = FunctionRunner._summarize_value(value)
            elif value is not None and not step.get("event_stop_requested"):
                detail = FunctionRunner._summarize_value(value)

            if detail:
                status = f"{status}: {detail}"
            steps.append(f"{block_type}={status}")
        return ", ".join(steps)

    @staticmethod
    def format_variable_accesses(accesses) -> list:
        """Variablenzugriffe lesbar: 'read Name=Wert'."""

        return [
            "{0} {1}{2}={3}".format(
                entry.get("operation", "read"),
                f"{entry['scope']}:" if entry.get("scope") else "",
                entry.get("name", ""),
                FunctionRunner._summarize_value(entry.get("value")),
            )
            for entry in accesses or []
        ]

    @staticmethod
    def _summarize_value(value):
        """Keep log lines short: full match/region dicts are still in the trace payload."""

        if isinstance(value, dict):
            return {key: value[key] for key in ("x", "y", "text") if key in value} or f"dict[{len(value)}]"

        if isinstance(value, (list, tuple)):
            return f"{type(value).__name__}[{len(value)}]"

        if isinstance(value, str) and len(value) > 120:
            return value[:120] + "..."

        return value