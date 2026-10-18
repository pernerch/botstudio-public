import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from models.bot_function import BotFunction
from models.function_block import FunctionBlock
from models.function_connection import FunctionConnection
from services.function_block_catalog import BLOCK_FAILURE, BLOCK_SUCCESS
from services.function_end_audit import audit_game, block_outcomes, open_failure_ends


def _function(name="F", kind="regular"):
    return BotFunction.create(name=name, game_id="g", author="t", kind=kind)


class _FakeService:
    def __init__(self, functions):
        self.functions = {f.function_id: f for f in functions}

    def load(self, game_id, function_id):
        return self.functions.get(function_id)

    def list_functions(self, game_id):
        return list(self.functions.values())


def test_unwired_failed_output_is_an_open_end():
    function = _function()
    tap = FunctionBlock.create("tap_point")
    function.blocks.append(tap)
    ends = open_failure_ends(function)
    assert [(e["block_id"], e["outcome"]) for e in ends] == [(tap.block_id, "failed")]


def test_wired_by_connection_or_branch_is_not_open():
    function = _function()
    tap = FunctionBlock.create("tap_point")
    end = FunctionBlock.create(BLOCK_SUCCESS)
    find = FunctionBlock.create("find_shape", branches={"not_found": [end]})
    function.blocks.extend([tap, end, find])
    function.connections.append(FunctionConnection(tap.block_id, "failed", end.block_id))
    assert open_failure_ends(function) == []


def test_failure_terminal_and_splitter_are_never_open():
    function = _function()
    function.blocks.append(FunctionBlock.create(BLOCK_FAILURE))
    function.blocks.append(FunctionBlock.create("splitter", params={"outputs": 3}))
    assert open_failure_ends(function) == []


def test_call_function_uses_helper_outcomes():
    helper = _function("H", kind="helper")
    helper.interface.outcomes = ["ok", "custom"]
    caller = _function("C")
    call = FunctionBlock.create("call_function", params={"function_id": helper.function_id})
    caller.blocks.append(call)
    service = _FakeService([helper, caller])
    assert block_outcomes(call, "g", service) == ["ok", "custom"]
    assert open_failure_ends(caller, service) == []


def test_audit_game_sorts_worst_first_and_includes_helpers():
    clean = _function("Clean")
    helper = _function("Helper", kind="helper")
    helper.blocks.extend([FunctionBlock.create("tap_point"), FunctionBlock.create("swipe")])
    one = _function("One")
    one.blocks.append(FunctionBlock.create("tap_point"))
    rows = audit_game(_FakeService([clean, one, helper]), "g")
    assert [(r["name"], r["open_ends"]) for r in rows] == [
        ("Helper", 2), ("One", 1), ("Clean", 0)
    ]
    assert rows[0]["kind"] == "helper"


def test_canvas_marks_open_ends_and_reports_count():
    from PySide6.QtWidgets import QApplication
    from ui.views.function_editor_view import FunctionCanvas

    QApplication.instance() or QApplication([])
    canvas = FunctionCanvas()
    reported = []
    canvas.open_ends_changed.connect(lambda count: reported.append(count))
    tap = FunctionBlock.create("tap_point")
    find = FunctionBlock.create("find_shape")
    end = FunctionBlock.create(BLOCK_SUCCESS)
    canvas.draw_blocks(
        [tap, find, end],
        [FunctionConnection(tap.block_id, "failed", end.block_id)],
    )
    assert canvas.open_end_count == 1
    assert reported[-1] == 1
    marked = [
        (item.data(0), item.data(2))
        for item in canvas.scene_model.items()
        if item.data(3) == "open_end"
    ]
    assert marked == [(find.block_id, "not_found")]


def test_editor_one_click_wires_open_end_to_end_block():
    from PySide6.QtWidgets import QApplication
    from ui.views.function_editor_view import FunctionEditorView

    QApplication.instance() or QApplication([])
    view = FunctionEditorView()
    function = _function()
    tap = FunctionBlock.create("tap_point")
    function.blocks.append(tap)
    view.current_function = function
    view.canvas.current_function = function
    view.canvas.draw_blocks(function.blocks, function.connections)
    assert view.canvas.open_end_count == 1

    view.wire_to_end_terminal(tap.block_id, "failed", BLOCK_FAILURE)

    terminal = function.blocks[-1]
    assert terminal.block_type == BLOCK_FAILURE
    assert any(
        c.source_block_id == tap.block_id
        and c.source_outcome == "failed"
        and c.target_block_id == terminal.block_id
        for c in function.connections
    )
    assert view.canvas.open_end_count == 0


def test_editor_reuses_existing_end_block_unless_new_requested():
    from PySide6.QtWidgets import QApplication
    from ui.views.function_editor_view import FunctionEditorView

    QApplication.instance() or QApplication([])
    view = FunctionEditorView()
    function = _function()
    tap_a = FunctionBlock.create("tap_point")
    tap_b = FunctionBlock.create("tap_point")
    function.blocks.extend([tap_a, tap_b])
    view.current_function = function
    view.canvas.current_function = function
    view.canvas.draw_blocks(function.blocks, function.connections)

    view.wire_to_end_terminal(tap_a.block_id, "failed", BLOCK_FAILURE)
    view.wire_to_end_terminal(tap_b.block_id, "failed", BLOCK_FAILURE)
    terminals = [b for b in function.blocks if b.block_type == BLOCK_FAILURE]
    assert len(terminals) == 1
    assert {
        c.source_block_id
        for c in function.connections
        if c.target_block_id == terminals[0].block_id
    } == {tap_a.block_id, tap_b.block_id}

    view.wire_to_end_terminal(tap_b.block_id, "failed", BLOCK_FAILURE, True)
    assert len([b for b in function.blocks if b.block_type == BLOCK_FAILURE]) == 2
