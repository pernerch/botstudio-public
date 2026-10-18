"""Deleted/recreated functions that are still referenced must be reported."""

from types import SimpleNamespace

from models.bot_function import BotFunction
from models.function_block import FunctionBlock
from scheduler.models import Event, Task
from services.dependency_service import DependencyService


class _Functions:
    def __init__(self, functions):
        self._functions = functions

    def list_functions(self, game_id):
        return list(self._functions)


class _Scheduler:
    def __init__(self, events):
        self._events = events

    def list_events(self, game_id):
        return list(self._events)


def _service(functions, events):
    return DependencyService(
        function_service=_Functions(functions),
        variable_service=SimpleNamespace(list_globals=lambda game_id: []),
        library_service=SimpleNamespace(),
        scheduler_service=_Scheduler(events),
    )


def test_inventory_reports_missing_task_and_call_function_references():
    replacement = BotFunction(
        function_id="new-open-daily",
        name="H_LSM_Open_Daily",
        game_id="g",
    )
    caller = BotFunction(function_id="caller", name="CollectStamina", game_id="g")
    caller.blocks = [
        FunctionBlock(
            block_id="b1",
            block_type="call_function",
            name="H_LSM_Open_Daily",
            params={"function_id": "old-open-daily"},
        )
    ]
    event = Event(event_id="ev", name="Training")
    event.tasks = [
        Task(task_id="t1", name="TrainMarkman", function_ids=["gone-task-fn"]),
        Task(task_id="t2", name="Fine", function_ids=["new-open-daily"]),
    ]

    inventory = _service([replacement, caller], [event]).build_inventory("g")
    missing = inventory["missing_functions"]

    assert set(missing) == {"old-open-daily", "gone-task-fn"}
    assert missing["old-open-daily"]["label"] == "H_LSM_Open_Daily"
    assert missing["old-open-daily"]["users"][0]["name"] == "CollectStamina"
    assert missing["gone-task-fn"]["label"] == ""
    assert missing["gone-task-fn"]["users"][0]["name"] == "TrainMarkman"
    assert missing["gone-task-fn"]["users"][0]["event_name"] == "Training"


def test_inventory_has_no_missing_entries_when_all_references_exist():
    function = BotFunction(function_id="f", name="F", game_id="g")
    event = Event(event_id="ev", name="E")
    event.tasks = [Task(task_id="t", name="T", function_ids=["f"])]

    inventory = _service([function], [event]).build_inventory("g")

    assert inventory["missing_functions"] == {}


def test_scheduler_tree_keeps_missing_marking_on_in_place_refresh(monkeypatch):
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem

    import services.reference_log as reference_log
    from ui.views.scheduler_view import SchedulerView

    logged = []
    monkeypatch.setattr(reference_log, "_LOGGED", set())
    monkeypatch.setattr(
        reference_log,
        "get_logger",
        lambda name: SimpleNamespace(warning=lambda msg, **kw: logged.append(kw)),
    )
    QApplication.instance() or QApplication([])
    tree = QTreeWidget()
    tree.setColumnCount(6)
    view = SimpleNamespace(tree=tree, game_id="g")
    task_item = QTreeWidgetItem(["TrainMarkman", "1", "", "active", "yes", ""])
    task_item.setData(0, 0x0100, {"kind": "task", "event_id": "ev", "task_id": "t"})
    task = Task(task_id="t", name="TrainMarkman", function_ids=["ok", "9b5e47e0-xyz"])

    marked = SchedulerView._apply_missing_functions(
        view, task_item, task, {"ok": "Okay"}
    )

    assert marked is True
    assert "9b5e47e0" not in task_item.text(2)
    assert "9b5e47e0" not in task_item.toolTip(0)
    assert task_item.text(2).startswith("Okay | ")
    assert task_item.text(3) == "inactive"
    assert task_item.foreground(0).color() == QColor("#d00000")
    assert logged[0]["missing_function_id"] == "9b5e47e0-xyz"
    assert logged[0]["open_target"] == {
        "kind": "task", "game_id": "g", "event_id": "ev", "task_id": "t",
    }

    task.function_ids = ["ok"]
    assert SchedulerView._apply_missing_functions(
        view, task_item, task, {"ok": "Okay"}
    ) is False
    assert task_item.toolTip(0) == ""


def test_missing_tab_opens_tasks_and_functions_without_showing_ids(monkeypatch):
    from PySide6.QtWidgets import QApplication

    import services.reference_log as reference_log
    from ui.views.dependencies_view import DependenciesView

    logged = []
    monkeypatch.setattr(reference_log, "_LOGGED", set())
    monkeypatch.setattr(
        reference_log,
        "get_logger",
        lambda name: SimpleNamespace(warning=lambda msg, **kw: logged.append(kw)),
    )
    QApplication.instance() or QApplication([])
    view = DependenciesView("")
    view._inventory = {
        "missing_functions": {
            "9b5e47e0-4e57": {
                "label": "",
                "users": [
                    {"kind": "function", "id": "caller", "name": "CollectStamina"},
                    {
                        "kind": "task", "id": "t1", "name": "Train",
                        "event_id": "ev", "event_name": "E",
                        "profile_name": "Marty McSpy",
                    },
                ],
            }
        }
    }
    view._render_missing_functions()
    functions, tasks = [], []
    view.open_function_requested.connect(
        lambda game_id, function_id: functions.append(function_id)
    )
    view.open_task_requested.connect(
        lambda game_id, event_id, task_id: tasks.append((event_id, task_id))
    )
    parent = view.missing_tree.topLevelItem(0)
    function_child, task_child = parent.child(0), parent.child(1)

    assert view.missing_tree.columnCount() == 2
    for item in (parent, function_child, task_child):
        for column in range(2):
            assert "9b5e47e0" not in item.text(column)
    assert "Marty McSpy" in task_child.text(0)
    ours = lambda: [kw for kw in logged if kw["missing_function_id"] == "9b5e47e0-4e57"]
    assert len(ours()) == 2

    view.missing_tree.setCurrentItem(task_child)
    assert view.missing_open_button.isEnabled()
    view.missing_open_button.click()
    assert tasks == [("ev", "t1")]

    view.missing_tree.setCurrentItem(function_child)
    view.missing_tree.itemDoubleClicked.emit(function_child, 0)
    assert functions == ["caller"]

    view._render_missing_functions()
    assert len(ours()) == 2


def test_inventory_task_users_carry_profile_name(monkeypatch):
    monkeypatch.setattr(
        DependencyService, "_profile_names", staticmethod(lambda: {"p1": "Marty"})
    )
    event = Event(event_id="ev", name="E", profile_id="p1")
    event.tasks = [Task(task_id="t", name="T", function_ids=["gone"])]

    inventory = _service([], [event]).build_inventory("g")

    user = inventory["missing_functions"]["gone"]["users"][0]
    assert user["profile_name"] == "Marty"
    assert user["event_id"] == "ev"


def test_log_record_open_target_is_detected():
    from ui.views.log_view import LogView

    target = {"kind": "task", "game_id": "g", "event_id": "e", "task_id": "t"}
    assert LogView.record_open_target({"details": {"open_target": target}}) == target
    assert LogView.record_open_target({"details": {}}) is None
    assert LogView.record_open_target({"details": {"open_target": {"kind": "x"}}}) is None
    assert LogView.record_open_target(None) is None


def test_scheduler_reveal_task_selects_pending_task():
    from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem

    from ui.views.scheduler_view import SchedulerView

    QApplication.instance() or QApplication([])
    tree = QTreeWidget()
    profile = QTreeWidgetItem(["Marty"])
    event = QTreeWidgetItem(["E"])
    event.setData(0, 0x0100, {"kind": "event", "event_id": "ev"})
    task = QTreeWidgetItem(["T"])
    task.setData(0, 0x0100, {"kind": "task", "event_id": "ev", "task_id": "t"})
    tree.addTopLevelItem(profile)
    profile.addChild(event)
    event.addChild(task)
    view = SimpleNamespace(tree=tree, _pending_reveal=("ev", "t"))

    assert SchedulerView._apply_pending_reveal(view) is True
    assert tree.currentItem() is task
    assert profile.isExpanded() and event.isExpanded()
    assert view._pending_reveal is None
    assert SchedulerView._apply_pending_reveal(view) is False


def test_scheduler_reveal_reports_hidden_task():
    from PySide6.QtWidgets import QApplication, QLabel, QTreeWidget

    from ui.views.scheduler_view import SchedulerView

    QApplication.instance() or QApplication([])
    label = QLabel()
    view = SimpleNamespace(
        tree=QTreeWidget(), _pending_reveal=("ev", "t"), action_feedback_label=label
    )

    assert SchedulerView._apply_pending_reveal(view) is False
    assert view._pending_reveal == ("ev", "t")
    assert label.text() == ""

    assert SchedulerView._apply_pending_reveal(view, final=True) is False
    assert view._pending_reveal is None
    assert label.text()


def test_inventory_marks_disabled_profiles(monkeypatch):
    monkeypatch.setattr(
        DependencyService, "_profile_names", staticmethod(lambda: {"p1": "Happy"})
    )
    monkeypatch.setattr(
        DependencyService, "_disabled_profile_ids", staticmethod(lambda: {"p1"})
    )
    event = Event(event_id="ev", name="E", profile_id="p1")
    event.tasks = [Task(task_id="t", name="T", function_ids=["gone"])]

    user = _service([], [event]).build_inventory("g")["missing_functions"]["gone"]["users"][0]

    assert user["profile_enabled"] is False
    from ui.views.dependencies_view import DependenciesView

    text = DependenciesView._missing_user_text(user)
    assert "'Happy'" in text and "Happy'" in text
