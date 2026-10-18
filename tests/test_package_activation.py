from contextlib import contextmanager
import faulthandler
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, get_ident
import time
from unittest import TestCase, main
from unittest.mock import Mock, patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from models.bot_function import BotFunction
from models.function_block import FunctionBlock
from models.github_account import GitHubAccount
from scheduler.models import Event, Task, SchedulerService
from services.application_bootstrap import initialize_application
from services.application_paths import ApplicationPaths
from services.file_io import atomic_write_json, read_json
from services.function_service import FunctionService
from services.library_service import LibraryService
from services.managed_package_service import ManagedPackageService
from services.package_activation_service import PackageActivationService
from services.package_service import PackageError, PackageService
from services.reinitialize_service import ReinitializeService
from services.runtime_service import RuntimeService
from services.runtime_supervisor import RuntimeSupervisor
from services.variable_service import VariableService
from ui.i18n import translator
from ui.i18n.translator import set_language, t
from ui.widgets.managed_packages_dialog import ManagedPackagesDialog
from ui.views.function_editor_view import FunctionEditorView


class PackageActivationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        started = time.monotonic()
        print(f"TEST START: {self.id()}")
        faulthandler.dump_traceback_later(20, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(f"TEST END: {self.id()} duration {time.monotonic() - started:.3f}s"))
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        atomic_write_json(self.root / "games/games.json", [{"id": "game"}])
        atomic_write_json(self.root / "profiles/profiles.json", [
            {"id": "local", "botstudio_profile_id": "technical-local", "game": "game", "name": "Local"},
            {"id": "wrong-game", "game": "another", "name": "Other"},
        ])
        self.packages = PackageService(
            function_root=str(self.root / "source-functions"),
            library_service=Mock(), variable_service=Mock(), game_service=Mock(),
        )
        self.managed = ManagedPackageService(self.root / "data/packages", self.packages)
        self.guard_entries = []
        @contextmanager
        def guard():
            self.guard_entries.append(True)
            yield
        self.activation = PackageActivationService(
            root=self.root, managed=self.managed, execution_guard=guard, logger=Mock(),
        )
        self.creator = GitHubAccount("123", "creator")
        self.helper = BotFunction.create("Helper", "game", "", kind="helper")
        self.helper.function_id = "helper"
        self.helper.saved = True
        self.function = BotFunction.create("Function", "game", "")
        self.function.function_id = "function"
        self.function.bump_revision("", account=self.creator)
        self.function.saved = True
        self.function.blocks = [
            FunctionBlock.create("call_function", params={"function_id": "helper"}),
            FunctionBlock.create("find_element", params={"library_entry_id": "image"}),
        ]
        self.event = Event(
            event_id="event", game_id="game", profile_id="foreign", name="Event",
            enabled=True, status="queued", next_due_at="2020-01-01T00:00:00Z",
            last_started_at="old", last_finished_at="old", last_executed=["old"],
            duration_samples_seconds=[10],
            tasks=[Task(
                task_id="task", function_ids=["function"], status="running",
                last_started_at="old", last_finished_at="old", duration_samples_seconds=[2],
            )],
        )
        self.library = {
            "entry_id": "image", "name": "Image", "boundary": {},
            "pattern": {"reference_path": r"C:\foreign\data\library\image\pattern_reference.png"},
            "accepted_samples": [{
                "source": r"C:\foreign\data\library\image\accepted\001_source.png",
                "crop": r"C:\foreign\data\library\image\accepted\001_crop.png",
            }],
            "rejected_samples": [], "used_by": ["foreign-function"],
        }
        self.definitions = [{
            "name": "setting", "variable_id": "setting-id", "schema_version": 2,
            "kind": "config", "value_type": "number", "initial_value": 5,
            "writable_by_functions": False,
        }]

    def install(self, *, event=True, definitions=None, mutate=None, version="1.0.0", store=True):
        content = {
            "content/functions/game/function.json": self.function.to_dict(),
            "content/functions/game/helper.json": self.helper.to_dict(),
            "content/library/image/entry.json": self.library,
            "content/variables/game/variables.json": self.definitions if definitions is None else definitions,
            "content/variables/game/profiles/foreign.json": {
                "values": {"secret": 100}, "freshness": {"secret": True},
            },
        }
        if event:
            content["content/scheduler/game/event.json"] = self.event.to_dict()
        content = {name: json.dumps(data).encode("utf-8") for name, data in content.items()}
        image = QImage(2, 2, QImage.Format_RGB32)
        image.fill(0xFFFFFF)
        self.assertTrue(image.save(str(self.root / "asset.png")))
        for name in ("pattern_reference.png", "accepted/001_source.png", "accepted/001_crop.png"):
            content[f"content/library/image/{name}"] = (self.root / "asset.png").read_bytes()
        if mutate:
            mutate(content)
        checksums = {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
        manifest = {
            "format": "botstudio-package", "format_version": 1,
            "package_name": "Fixture", "description": "", "game": {"id": "game"},
            "contents": {"functions": 2}, "checksums": checksums,
            "package_metadata": {
                "package_id": "example", "package_version": version,
                "publisher": self.creator.to_dict(), "author": "Creator",
                "license_id": "MIT", "license_text": "Fixture terms", "rights_confirmed": True,
            },
        }
        archive = self.root / "package.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("manifest.json", json.dumps(manifest))
            output.writestr("checksums.json", json.dumps(checksums))
            for name, data in content.items():
                output.writestr(name, data)
        return self.managed.install(archive) if store else archive

    def active_snapshot(self):
        result = {}
        for area in ("functions", "library", "variables", "scheduler", "package-activations"):
            directory = self.root / "data" / area
            if directory.exists():
                result.update({
                    path.relative_to(self.root).as_posix(): path.read_bytes()
                    for path in directory.rglob("*") if path.is_file()
                })
        return result

    def prepare(self, status, profile="technical-local"):
        return self.activation.prepare(status["installation_id"], profile)

    def activate(self, status, plan):
        return self.activation.activate(status["installation_id"], plan["profile_id"], plan["plan_sha256"])

    def test_active_import_roundtrip_assets_provenance_dependencies_and_disabled_events(self):
        status = self.install()
        original = Path(status["original_archive"]).read_bytes()
        baseline = ManagedPackageService._snapshot(Path(status["working_directory"]))
        context = self.activation.context(status["installation_id"])
        self.assertTrue(context["has_events"])
        self.assertEqual(["technical-local"], [profile["id"] for profile in context["profiles"]])
        plan = self.prepare(status)
        self.assertEqual({}, self.active_snapshot())
        self.assertEqual(plan, self.prepare(status), "same content must produce stable consent")
        result = self.activate(status, plan)
        self.assertEqual([True], self.guard_entries)
        self.assertEqual(2, result["functions"])
        self.assertEqual(plan["files"], len(self.active_snapshot()))
        bus = ReinitializeService()
        library = LibraryService(root=self.root / "data/library", reinitialize_service=bus)
        variables = VariableService(root=str(self.root / "data/variables"), logger=Mock(), reinitialize_service=bus)
        functions = FunctionService(
            root=str(self.root / "data/functions"), library_service=library,
            variable_service=variables, logger=Mock(), reinitialize_service=bus,
        )
        restored = functions.load("game", "function")
        self.assertEqual(self.creator, restored.creator_account)
        self.assertEqual(self.creator, restored.history[-1].account)
        self.assertEqual("example", restored.package_origins[-1].package_id)
        self.assertEqual(["function"], functions.load("game", "helper").used_by)
        entry = library.load("image")
        self.assertEqual(["function"], entry["used_by"])
        self.assertTrue(Path(entry["pattern"]["reference_path"]).is_file())
        self.assertFalse(QImage(entry["pattern"]["reference_path"]).isNull())
        self.assertTrue(Path(entry["accepted_samples"][0]["source"]).is_file())
        scheduler = SchedulerService(
            base_path=str(self.root / "data/scheduler"), logger=Mock(), reinitialize_service=bus,
        )
        event = scheduler.load_event("game", "event")
        self.assertFalse(event.enabled)
        self.assertEqual("technical-local", event.profile_id)
        self.assertEqual("idle", event.status)
        self.assertIsNone(event.next_due_at)
        self.assertIsNone(event.last_started_at)
        self.assertEqual([], event.last_executed)
        self.assertEqual([], event.tasks[0].duration_samples_seconds)
        self.assertEqual("idle", event.tasks[0].status)
        self.assertFalse((self.root / "data/variables/game/profiles").exists())
        self.assertEqual(baseline, ManagedPackageService._snapshot(Path(status["working_directory"])))
        self.assertEqual(original, Path(status["original_archive"]).read_bytes())
        receipt = read_json(self.root / f"data/package-activations/{status['installation_id']}.json")
        self.assertEqual("1.0.0", receipt["version"])
        self.assertIn("data/functions/game/function.json", receipt["active_files"])
        before = self.active_snapshot()
        with self.assertRaisesRegex(PackageError, "already exists"):
            self.prepare(status)
        self.assertEqual(before, self.active_snapshot())

    def test_conflicts_fail_before_any_writes(self):
        status = self.install()
        for relative in ("functions/game/function.json", "library/image/entry.json", "scheduler/game/event.json"):
            with self.subTest(relative=relative):
                target = self.root / "data" / relative
                atomic_write_json(target, {"local": True})
                before = self.active_snapshot()
                with self.assertRaisesRegex(PackageError, "already exists"):
                    self.prepare(status)
                self.assertEqual(before, self.active_snapshot())
                target.unlink()
                if relative.startswith("library"):
                    target.parent.rmdir()

    def test_variables_merge_identical_definitions_and_reject_conflicts_without_profile_values(self):
        status = self.install()
        registry = self.root / "data/variables/game/variables.json"
        atomic_write_json(registry, self.definitions + [{
            **self.definitions[0], "name": "local-only", "variable_id": "local-id",
        }])
        profile = self.root / "data/variables/game/profiles/technical-local.json"
        atomic_write_json(profile, {"values": {"setting": 77}, "freshness": {"setting": True}})
        before_profile = profile.read_bytes()
        plan = self.prepare(status)
        self.activate(status, plan)
        names = {entry["name"] for entry in read_json(registry)}
        self.assertEqual({"setting", "local-only", "Event"}, names)
        self.assertEqual(before_profile, profile.read_bytes())

    def test_conflicting_variable_types_and_invalid_profiles_are_rejected(self):
        status = self.install()
        registry = self.root / "data/variables/game/variables.json"
        atomic_write_json(registry, [{**self.definitions[0], "value_type": "text", "initial_value": "local"}])
        before = self.active_snapshot()
        with self.assertRaisesRegex(PackageError, "variable definition conflicts"):
            self.prepare(status)
        self.assertEqual(before, self.active_snapshot())
        registry.unlink()
        for profile in ("", "foreign", "wrong-game"):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(PackageError, "local profile"):
                    self.prepare(status, profile)
        atomic_write_json(self.root / "games/games.json", [])
        with self.assertRaisesRegex(PackageError, "game locally"):
            self.prepare(status)

    def test_foreign_state_system_events_and_incomplete_dependencies_are_rejected(self):
        def edit(name, field, value):
            def mutate(content):
                data = json.loads(content[name])
                data[field] = value
                content[name] = json.dumps(data).encode()
            return mutate
        cases = [
            edit("content/scheduler/game/event.json", "is_init", True),
            edit("content/scheduler/game/event.json", "is_interrupt", True),
            lambda content: content.pop("content/functions/game/helper.json"),
            lambda content: content.pop("content/library/image/pattern_reference.png"),
        ]
        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                with TemporaryDirectory() as other:
                    old_managed = self.managed
                    self.managed = ManagedPackageService(Path(other) / "managed", self.packages)
                    self.activation.managed = self.managed
                    try:
                        status = self.install(mutate=mutate)
                        with self.assertRaises((PackageError, ValueError)):
                            self.prepare(status)
                        self.assertEqual({}, self.active_snapshot())
                    finally:
                        self.managed = old_managed
                        self.activation.managed = old_managed

    def test_live_working_edits_are_importable_but_changes_after_consent_are_rejected(self):
        status = self.install(event=False)
        source = Path(status["working_directory"]) / "content/functions/game/function.json"
        data = read_json(source)
        data["name"] = "Local fix"
        atomic_write_json(source, data)
        plan = self.prepare(status, "")
        self.assertTrue(plan["modified"])
        data["description"] = "Changed after preview"
        atomic_write_json(source, data)
        with self.assertRaisesRegex(PackageError, "changed since activation preview"):
            self.activate(status, plan)
        self.assertEqual({}, self.active_snapshot())
        plan = self.prepare(status, "")
        self.activate(status, plan)
        self.assertEqual("Local fix", read_json(self.root / "data/functions/game/function.json")["name"])

    def test_local_registry_or_profile_changes_after_preview_require_new_consent(self):
        status = self.install()
        plan = self.prepare(status)
        atomic_write_json(self.root / "data/variables/game/variables.json", [{
            **self.definitions[0], "name": "added", "variable_id": "added-id",
        }])
        before = self.active_snapshot()
        with self.assertRaisesRegex(PackageError, "changed since activation preview"):
            self.activate(status, plan)
        self.assertEqual(before, self.active_snapshot())
        plan = self.prepare(status)
        atomic_write_json(self.root / "profiles/profiles.json", [])
        with self.assertRaisesRegex(PackageError, "local profile"):
            self.activate(status, plan)
        self.assertEqual(before, self.active_snapshot())

    def test_write_failure_rolls_back_new_files_and_existing_registry(self):
        status = self.install()
        registry = self.root / "data/variables/game/variables.json"
        atomic_write_json(registry, [])
        before = self.active_snapshot()
        plan = self.prepare(status)
        original_replace = os.replace
        def fail_receipt(source, target):
            if str(target).endswith("pending.json"):
                data = read_json(Path(source))
                if data.get("committed") is True:
                    raise OSError("fixture completion write failed")
            return original_replace(source, target)
        with patch("services.package_activation_service.os.replace", side_effect=fail_receipt):
            with self.assertRaisesRegex(OSError, "completion write failed"):
                self.activate(status, plan)
        self.assertEqual(before, self.active_snapshot())
        self.assertFalse((self.activation.journal_root / "pending.json").exists())
        self.assertFalse((self.root / "data/library/image").exists())
        self.assertEqual(plan, self.prepare(status), "rolled-back activation must remain retryable")

    def test_interrupted_transaction_is_recovered_before_application_start(self):
        status = self.install()
        plan = self.prepare(status)
        original_link = os.link
        count = []
        def interrupt(source, target):
            original_link(source, target)
            count.append(True)
            if len(count) == 2:
                raise KeyboardInterrupt("simulated abrupt shutdown")
        with patch("services.package_activation_service.os.link", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.activate(status, plan)
        self.assertTrue(self.active_snapshot())
        self.assertTrue((self.activation.journal_root / "pending.json").exists())
        initialize_application(ApplicationPaths(self.root, self.root, self.root), activate=False)
        self.assertEqual({}, self.active_snapshot())
        self.assertFalse((self.activation.journal_root / "pending.json").exists())

    def test_recovery_never_discards_a_later_local_edit(self):
        target = self.root / "data/functions/game/function.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"later local edit")
        atomic_write_json(self.activation.journal_root / "pending.json", {
            "schema_version": 1, "committed": False,
            "files": [{"path": "data/functions/game/function.json", "before": None,
                       "after": hashlib.sha256(b"imported").hexdigest()}],
        })
        with self.assertRaisesRegex(PackageError, "overwrite changed local data"):
            self.activation.recover()
        self.assertEqual(b"later local edit", target.read_bytes())
        self.assertTrue((self.activation.journal_root / "pending.json").exists())

    def test_committed_cleanup_failure_reports_that_content_is_already_active(self):
        status = self.install()
        plan = self.prepare(status)
        original_unlink = Path.unlink
        def fail_journal_cleanup(path, *args, **kwargs):
            if path == self.activation.journal_root / "pending.json":
                raise PermissionError("fixture cleanup denied")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_journal_cleanup):
            with self.assertRaisesRegex(PackageError, "content was activated"):
                self.activate(status, plan)
        active = self.active_snapshot()
        self.assertTrue(active)
        self.assertTrue(read_json(self.activation.journal_root / "pending.json")["committed"])
        self.activation.recover()
        self.assertEqual(active, self.active_snapshot())
        self.assertFalse((self.activation.journal_root / "pending.json").exists())

    def test_runtime_guard_rejects_live_execution_and_prevents_remote_start(self):
        runtime = RuntimeService.__new__(RuntimeService)
        runtime._start_lock = Lock()
        runtime.running = False
        runtime.heartbeat = Mock(wait_until_idle=Mock(return_value=True))
        runtime._profile_execution_locks = {}
        runtime.execution_activity = Mock(snapshot=Mock(return_value=[]))
        with runtime.content_import_guard():
            self.assertTrue(runtime._start_lock.locked())
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                runtime.start()
        self.assertFalse(runtime._start_lock.locked())
        runtime.running = True
        with self.assertRaisesRegex(RuntimeError, "Stop Runtime"):
            with runtime.content_import_guard():
                self.fail("running Runtime must block active import")
        self.assertFalse(runtime._start_lock.locked())
        runtime.running = False
        runtime.execution_activity.snapshot.return_value = [{"active": True}]
        with self.assertRaisesRegex(RuntimeError, "still active"):
            with runtime.content_import_guard():
                self.fail("active execution must block import")
        runtime.execution_activity.snapshot.return_value = []
        runtime.heartbeat.wait_until_idle.return_value = False
        with self.assertRaisesRegex(RuntimeError, "Stop Runtime"):
            with runtime.content_import_guard():
                self.fail("stopping heartbeat must block import")
        runtime.heartbeat.wait_until_idle.return_value = True
        profile_lock = Lock()
        profile_lock.acquire()
        runtime._profile_execution_locks = {"profile": profile_lock}
        with self.assertRaisesRegex(RuntimeError, "still stopping"):
            with runtime.content_import_guard():
                self.fail("profile execution lock must block import")
        profile_lock.release()
        runtime._start_lock.acquire()
        with self.assertRaisesRegex(RuntimeError, "already in progress"):
            with runtime.content_import_guard():
                self.fail("concurrent startup must block import")
        runtime._start_lock.release()

    def test_field_and_event_generated_variable_ids_are_stable_between_preview_and_commit(self):
        self.function.blocks.append(FunctionBlock.create("configure_field", params={
            "variable_name": "Cooldown", "field_target": "task", "variable_kind": "live",
            "value_type": "duration", "writable_by_functions": True,
        }))
        status = self.install()
        plan = self.prepare(status)
        self.assertEqual(plan, self.prepare(status))
        self.activate(status, plan)
        function = BotFunction.from_dict(read_json(self.root / "data/functions/game/function.json"))
        definitions = read_json(self.root / "data/variables/game/variables.json")
        field = next(block for block in function.blocks if block.block_type == "configure_field")
        definition = next(entry for entry in definitions if entry["name"] == "Cooldown")
        self.assertEqual(definition["variable_id"], field.params["variable_id"])

    def test_configure_field_must_not_change_an_existing_local_definition(self):
        self.function.blocks.append(FunctionBlock.create("configure_field", params={
            "variable_name": "Cooldown", "field_target": "task", "variable_kind": "live",
            "value_type": "duration", "writable_by_functions": True,
        }))
        status = self.install(event=False)
        registry = self.root / "data/variables/game/variables.json"
        atomic_write_json(registry, [{
            "name": "Cooldown", "variable_id": "local-cooldown", "schema_version": 2,
            "kind": "live", "value_type": "duration", "writable_by_functions": True,
        }])
        before = self.active_snapshot()
        with self.assertRaisesRegex(PackageError, "conflict with local variable"):
            self.prepare(status, "")
        self.assertEqual(before, self.active_snapshot())

    def test_invalid_task_ids_flags_reserved_ids_and_asset_paths_are_rejected(self):
        status = self.install()
        event_path = Path(status["working_directory"]) / "content/scheduler/game/event.json"
        event = read_json(event_path)
        for task_id in (None, "", "../escape"):
            with self.subTest(task_id=task_id):
                event["tasks"][0]["task_id"] = task_id
                atomic_write_json(event_path, event)
                with self.assertRaises(PackageError):
                    self.prepare(status)
                self.assertEqual({}, self.active_snapshot())
        for flag in ("false", None, 1):
            with self.subTest(flag=flag):
                with self.assertRaises(PackageError):
                    self.activation._definitions("game", [{**self.definitions[0], "writable_by_functions": flag}])
        for value in ("CON", "NUL.txt", "bad.", "bad/part"):
            with self.subTest(identifier=value):
                with self.assertRaises(PackageError):
                    self.activation._identifier(value)
        for value in (r"image\C:\outside.png", r"image\..\outside.png"):
            with self.subTest(asset=value):
                with self.assertRaises(PackageError):
                    self.activation._asset_path(value, "image", self.root, self.root)

    def test_concurrent_new_target_is_preserved_and_prior_import_writes_are_rolled_back(self):
        status = self.install()
        plan = self.prepare(status)
        original_link = os.link
        attempts, foreign = [], []
        def concurrent_file(source, target):
            attempts.append(True)
            if len(attempts) == 2:
                Path(target).write_bytes(b"local concurrent content")
                foreign.append(Path(target).relative_to(self.root).as_posix())
            return original_link(source, target)
        with patch("services.package_activation_service.os.link", side_effect=concurrent_file):
            with self.assertRaises(FileExistsError):
                self.activate(status, plan)
        self.assertEqual({foreign[0]: b"local concurrent content"}, self.active_snapshot())
        self.assertFalse((self.activation.journal_root / "pending.json").exists())

    def test_invalid_recovery_journal_blocks_start_without_touching_content(self):
        target = self.root / "data/functions/game/function.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"local")
        journal_path = self.activation.journal_root / "pending.json"
        for journal in (
            {"schema_version": 1, "committed": "true", "files": []},
            {"schema_version": 1, "committed": False, "files": [{"path": "../outside"}]},
            {"schema_version": 1, "committed": False, "files": [], "started": -1},
        ):
            with self.subTest(journal=journal):
                atomic_write_json(journal_path, journal)
                with self.assertRaises(PackageError):
                    self.activation.recover()
                self.assertEqual(b"local", target.read_bytes())
                self.assertTrue(journal_path.exists())

    def test_pending_recovery_blocks_runtime_and_debug_before_any_worker_starts(self):
        runtime = RuntimeService.__new__(RuntimeService)
        runtime._start_lock = Lock()
        runtime.stop = Mock()
        runtime._start = Mock()
        supervisor = RuntimeSupervisor(profile_service=Mock(), worker_factory=Mock())
        with patch("services.runtime_service.Path") as runtime_path:
            runtime_path.return_value.exists.return_value = True
            with self.assertRaisesRegex(RuntimeError, "recovery is pending"):
                runtime.start()
        runtime._start.assert_not_called()
        self.assertFalse(runtime._start_lock.locked())
        with patch("services.runtime_supervisor.Path") as debug_path:
            debug_path.return_value.exists.return_value = True
            with self.assertRaisesRegex(RuntimeError, "recovery is pending"):
                supervisor.start_function("local", "function")
        supervisor.profile_service.get_profile.assert_not_called()
        supervisor.worker_factory.assert_not_called()
        editor = Mock(current_function=self.function)
        editor.supervisor.start_function.side_effect = RuntimeError("Package activation recovery is pending")
        FunctionEditorView._debug_run_after_save(editor, True, "local", None)
        self.assertIn("recovery is pending", editor.block_error.setText.call_args.args[0])
        editor.logger.error.assert_called_once()

    def test_archive_update_never_replaces_active_files_and_requires_separate_active_update_workflow(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        active = self.active_snapshot()
        self.function.name = "New upstream name"
        archive = self.install(version="1.1.0", store=False)
        updated = self.managed.update(status["installation_id"], archive)
        self.assertEqual("1.1.0", updated["version"])
        self.assertEqual(active, self.active_snapshot())
        with self.assertRaisesRegex(PackageError, "already exists"):
            self.prepare(updated)
        self.assertEqual(active, self.active_snapshot())

    def newer_archive(self, version="1.1.0"):
        archive = self.install(version=version, store=False)
        installation_id = self.managed.list_installations()[0]["installation_id"]
        return self.managed.update(installation_id, archive)

    def test_active_update_requires_separate_consent_and_preserves_origin_and_local_values(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        self.assertEqual("unchanged", self.activation.active_status(status["installation_id"])["state"])
        profile = self.root / "data/variables/game/profiles/technical-local.json"
        atomic_write_json(profile, {"values": {"setting": 99}})
        local = profile.read_bytes()
        self.function.name = "Fixed upstream"
        self.function.bump_revision("", account=self.creator)
        self.function.saved = True
        updated = self.newer_archive()
        active = self.active_snapshot()
        self.assertTrue(self.activation.active_status(status["installation_id"])["update_available"])
        plan = self.activation.prepare_update(status["installation_id"])
        self.assertEqual(active, self.active_snapshot())
        self.assertEqual(plan, self.activation.prepare_update(status["installation_id"]))
        self.assertEqual("update", plan["operation"])
        self.assertEqual("1.0.0", plan["previous_version"])
        self.assertEqual("technical-local", plan["profile_id"])
        self.activation.update(status["installation_id"], plan["plan_sha256"])
        data = read_json(self.root / "data/functions/game/function.json")
        self.assertEqual("Fixed upstream", data["name"])
        self.assertEqual(self.creator.to_dict(), data["creator_account"])
        self.assertEqual("1.1.0", data["package_origins"][-1]["package_version"])
        self.assertEqual(local, profile.read_bytes())
        receipt = read_json(self.activation._receipt_path(status["installation_id"]))
        self.assertEqual("1.1.0", receipt["version"])
        active_status = self.activation.active_status(status["installation_id"])
        self.assertEqual("unchanged", active_status["state"])
        self.assertFalse(active_status["update_available"])
        self.assertTrue(Path(updated["original_archive"]).is_file())
        with self.assertRaisesRegex(PackageError, "greater than"):
            self.activation.prepare_update(status["installation_id"])

    def test_local_function_library_event_and_definition_edits_block_active_updates(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        self.newer_archive()
        targets = [
            self.root / "data/functions/game/function.json",
            self.root / "data/library/image/pattern_reference.png",
            self.root / "data/scheduler/game/event.json",
            self.root / "data/variables/game/variables.json",
        ]
        for target in targets:
            original = target.read_bytes()
            with self.subTest(target=target.name):
                if target.name == "variables.json":
                    definitions = read_json(target)
                    definitions[0]["description"] = "local customization"
                    atomic_write_json(target, definitions)
                elif target.name == "event.json":
                    event = read_json(target)
                    event["enabled"] = True
                    atomic_write_json(target, event)
                else:
                    target.write_bytes(original + b" ")
                before = self.active_snapshot()
                self.assertEqual("modified", self.activation.active_status(status["installation_id"])["state"])
                with self.assertRaisesRegex(PackageError, "local active content changed"):
                    self.activation.prepare_update(status["installation_id"])
                self.assertEqual(before, self.active_snapshot())
                target.write_bytes(original)
        extra = self.root / "data/library/image/extra.png"
        extra.write_bytes(b"local sample")
        with self.assertRaisesRegex(PackageError, "extra.png"):
            self.activation.prepare_update(status["installation_id"])

    def test_active_update_preserves_unrelated_local_variable_definitions(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        registry = self.root / "data/variables/game/variables.json"
        definitions = read_json(registry)
        definitions.append({**self.definitions[0], "name": "Local variable", "variable_id": "local-variable"})
        atomic_write_json(registry, definitions)
        self.newer_archive()
        self.assertEqual("unchanged", self.activation.active_status(status["installation_id"])["state"])
        plan = self.activation.prepare_update(status["installation_id"])
        self.activation.update(status["installation_id"], plan["plan_sha256"])
        self.assertIn(definitions[-1], read_json(registry))

    def test_active_update_keeps_generated_field_ids_stable(self):
        self.function.blocks.append(FunctionBlock.create("configure_field", params={
            "variable_name": "Cooldown", "field_target": "task", "variable_kind": "live",
            "value_type": "duration", "writable_by_functions": True,
        }))
        status = self.install()
        self.activate(status, self.prepare(status))
        registry = self.root / "data/variables/game/variables.json"
        field_id = next(entry["variable_id"] for entry in read_json(registry) if entry["name"] == "Cooldown")
        self.function.name = "Field bugfix"
        self.newer_archive()
        plan = self.activation.prepare_update(status["installation_id"])
        self.activation.update(status["installation_id"], plan["plan_sha256"])
        function = BotFunction.from_dict(read_json(self.root / "data/functions/game/function.json"))
        field = next(block for block in function.blocks if block.block_type == "configure_field")
        self.assertEqual(field_id, field.params["variable_id"])
        self.assertEqual("unchanged", self.activation.active_status(status["installation_id"])["state"])

    def test_active_update_cannot_invent_a_profile_for_new_events(self):
        status = self.install(event=False)
        self.activate(status, self.prepare(status, ""))
        self.newer_archive()
        before = self.active_snapshot()
        with self.assertRaisesRegex(PackageError, "original local profile"):
            self.activation.prepare_update(status["installation_id"])
        self.assertEqual(before, self.active_snapshot())

    def test_active_update_blocks_missing_files_removals_and_shared_definition_migrations(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        self.newer_archive()
        target = self.root / "data/functions/game/function.json"
        before = target.read_bytes()
        target.unlink()
        with self.assertRaisesRegex(PackageError, "local active content changed"):
            self.activation.prepare_update(status["installation_id"])
        target.write_bytes(before)
        working = Path(self.managed.status(status["installation_id"])["working_directory"])
        # Deliver a valid higher-version package which no longer contains a former sample.
        def remove_sample(content):
            library = json.loads(content["content/library/image/entry.json"])
            library["accepted_samples"] = []
            content["content/library/image/entry.json"] = json.dumps(library).encode()
            content.pop("content/library/image/accepted/001_source.png")
            content.pop("content/library/image/accepted/001_crop.png")
        archive = self.install(version="1.2.0", store=False, mutate=remove_sample)
        self.managed.update(status["installation_id"], archive)
        active = self.active_snapshot()
        with self.assertRaisesRegex(PackageError, "removal requires migration"):
            self.activation.prepare_update(status["installation_id"])
        self.assertEqual(active, self.active_snapshot())
        self.assertTrue(working.is_dir())

    def test_active_update_rechecks_consent_and_rolls_back_existing_files_and_receipt(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        self.function.name = "Fixed"
        self.newer_archive()
        plan = self.activation.prepare_update(status["installation_id"])
        active = self.active_snapshot()
        original_replace = os.replace
        def fail_commit(source, target):
            if Path(target).name == "pending.json" and read_json(Path(source)).get("committed") is True:
                raise OSError("fixture update completion failed")
            return original_replace(source, target)
        with patch("services.package_activation_service.os.replace", side_effect=fail_commit):
            with self.assertRaisesRegex(OSError, "completion failed"):
                self.activation.update(status["installation_id"], plan["plan_sha256"])
        self.assertEqual(active, self.active_snapshot())
        self.assertEqual(plan, self.activation.prepare_update(status["installation_id"]))
        atomic_write_json(self.root / "profiles/profiles.json", [
            {"id": "local", "botstudio_profile_id": "technical-local", "game": "game", "name": "Changed"},
        ])
        with self.assertRaisesRegex(PackageError, "changed since activation preview"):
            self.activation.update(status["installation_id"], plan["plan_sha256"])
        self.assertEqual(active, self.active_snapshot())

    def test_active_update_adds_new_content_without_overwriting_unrelated_ids(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        extra = BotFunction.create("Added", "game", "")
        extra.function_id = "added"
        extra.saved = True
        def add_function(content):
            content["content/functions/game/added.json"] = json.dumps(extra.to_dict()).encode()
        archive = self.install(version="1.1.0", store=False, mutate=add_function)
        self.managed.update(status["installation_id"], archive)
        target = self.root / "data/functions/game/added.json"
        atomic_write_json(target, {"local": "unrelated"})
        before = self.active_snapshot()
        with self.assertRaisesRegex(PackageError, "identity already exists"):
            self.activation.prepare_update(status["installation_id"])
        self.assertEqual(before, self.active_snapshot())
        target.unlink()
        plan = self.activation.prepare_update(status["installation_id"])
        self.assertEqual(3, plan["functions"])
        self.activation.update(status["installation_id"], plan["plan_sha256"])
        self.assertEqual("Added", read_json(target)["name"])
        self.assertIn("data/functions/game/added.json", read_json(
            self.activation._receipt_path(status["installation_id"]),
        )["active_files"])

    def test_active_update_blocks_interface_and_variable_definition_migrations(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        self.function.interface.inputs = ["New required argument"]
        self.newer_archive()
        active = self.active_snapshot()
        with self.assertRaisesRegex(PackageError, "interface changes require migration"):
            self.activation.prepare_update(status["installation_id"])
        self.assertEqual(active, self.active_snapshot())
        self.function.interface.inputs = []
        self.definitions[0]["value_type"] = "text"
        self.definitions[0]["initial_value"] = "changed type"
        self.newer_archive("1.2.0")
        with self.assertRaisesRegex(PackageError, "variable definition conflicts"):
            self.activation.prepare_update(status["installation_id"])
        self.assertEqual(active, self.active_snapshot())

    def test_legacy_receipt_updates_only_with_unchanged_complete_registry(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        receipt_path = self.activation._receipt_path(status["installation_id"])
        receipt = read_json(receipt_path)
        receipt["schema_version"] = 1
        receipt.pop("variable_definitions")
        receipt.pop("publisher_key")
        atomic_write_json(receipt_path, receipt)
        self.newer_archive()
        registry = self.root / "data/variables/game/variables.json"
        original = registry.read_bytes()
        definitions = read_json(registry)
        definitions.append({**self.definitions[0], "name": "Local", "variable_id": "local"})
        atomic_write_json(registry, definitions)
        with self.assertRaisesRegex(PackageError, "local active content changed"):
            self.activation.prepare_update(status["installation_id"])
        registry.write_bytes(original)
        plan = self.activation.prepare_update(status["installation_id"])
        self.activation.update(status["installation_id"], plan["plan_sha256"])
        self.assertEqual(2, read_json(receipt_path)["schema_version"])
        self.assertEqual("unchanged", self.activation.active_status(status["installation_id"])["state"])

    def test_interrupted_active_update_restores_old_version_before_startup(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        self.function.name = "Updated"
        self.newer_archive()
        plan = self.activation.prepare_update(status["installation_id"])
        before = self.active_snapshot()
        original_replace = os.replace
        def interrupt_after_receipt(source, target):
            result = original_replace(source, target)
            if Path(target) == self.activation._receipt_path(status["installation_id"]):
                raise KeyboardInterrupt("fixture abrupt update interruption")
            return result
        with patch("services.package_activation_service.os.replace", side_effect=interrupt_after_receipt):
            with self.assertRaises(KeyboardInterrupt):
                self.activation.update(status["installation_id"], plan["plan_sha256"])
        self.assertNotEqual(before, self.active_snapshot())
        with self.assertRaisesRegex(PackageError, "recovery is pending"):
            self.activation.active_status(status["installation_id"])
        initialize_application(ApplicationPaths(self.root, self.root, self.root), activate=False)
        self.assertEqual(before, self.active_snapshot())
        self.assertEqual(plan, self.activation.prepare_update(status["installation_id"]))

    def test_active_update_refuses_different_identity_or_modified_working_copy(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        self.newer_archive()
        receipt_path = self.activation._receipt_path(status["installation_id"])
        original = receipt_path.read_bytes()
        receipt = read_json(receipt_path)
        receipt["publisher_key"] = "github:999"
        atomic_write_json(receipt_path, receipt)
        with self.assertRaisesRegex(PackageError, "different package identity"):
            self.activation.prepare_update(status["installation_id"])
        receipt_path.write_bytes(original)
        working = Path(self.managed.status(status["installation_id"])["working_directory"])
        (working / "local.txt").write_text("local edit")
        before = self.active_snapshot()
        with self.assertRaisesRegex(PackageError, "working-copy changes"):
            self.activation.prepare_update(status["installation_id"])
        self.assertEqual(before, self.active_snapshot())
        self.assertFalse(self.activation.active_status(status["installation_id"])["update_available"])

    def test_active_update_requires_new_consent_after_newer_archive_or_live_edit(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        self.newer_archive()
        plan = self.activation.prepare_update(status["installation_id"])
        self.newer_archive("1.2.0")
        before = self.active_snapshot()
        with self.assertRaisesRegex(PackageError, "changed since activation preview"):
            self.activation.update(status["installation_id"], plan["plan_sha256"])
        self.assertEqual(before, self.active_snapshot())
        plan = self.activation.prepare_update(status["installation_id"])
        path = self.root / "data/scheduler/game/event.json"
        data = read_json(path)
        data["priority"] += 1
        atomic_write_json(path, data)
        before = self.active_snapshot()
        with self.assertRaisesRegex(PackageError, "local active content changed"):
            self.activation.update(status["installation_id"], plan["plan_sha256"])
        self.assertEqual(before, self.active_snapshot())

    def wait(self, dialog):
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: loop.quit() if dialog.worker is None else None)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        timer.start()
        timeout.start(3000)
        try:
            loop.exec()
        finally:
            timer.stop()
            timeout.stop()
        self.assertIsNone(dialog.worker)

    def test_dialog_selects_profile_previews_and_requires_separate_confirmation(self):
        status = self.install()
        worker_threads = []
        for name in ("context", "prepare", "activate"):
            original = getattr(self.activation, name)
            def record_worker(*args, _call=original):
                worker_threads.append(get_ident())
                return _call(*args)
            setattr(self.activation, name, Mock(side_effect=record_worker))
        with patch("ui.widgets.managed_packages_dialog.get_logger", return_value=Mock()):
            dialog = ManagedPackagesDialog(service=self.managed, activation_service=self.activation)
        self.addCleanup(dialog.deleteLater)
        dialog.reload()
        self.wait(dialog)
        dialog.table.selectRow(0)
        self.assertTrue(dialog.activate.isEnabled())
        dialog._prepare_activation()
        self.wait(dialog)
        self.assertIsNotNone(dialog.confirmation)
        self.assertEqual({}, self.active_snapshot())
        dialog.confirmation.done(QDialog.Accepted)
        self.wait(dialog)
        self.assertEqual(
            QMessageBox.No, dialog.confirmation.standardButton(dialog.confirmation.defaultButton()),
        )
        self.assertIn("technical-local", dialog.details.toPlainText())
        self.assertEqual({}, self.active_snapshot())
        notifications, threads = [], []
        bus = ReinitializeService()
        def record(source, payload):
            notifications.append(source)
            threads.append(get_ident())
        bus.subscribe(record)
        with patch("ui.widgets.managed_packages_dialog.get_default_reinitialize_service", return_value=bus):
            dialog.confirmation.done(QMessageBox.Yes)
            self.wait(dialog)
        self.assertEqual(["library", "variable", "function", "scheduler"], notifications)
        self.assertEqual([get_ident()] * 4, threads)
        self.assertEqual(3, len(worker_threads))
        self.assertNotIn(get_ident(), worker_threads)
        self.assertEqual(t("packages.activation_complete"), dialog.status.text())
        self.assertEqual(status["installation_id"], read_json(
            self.root / f"data/package-activations/{status['installation_id']}.json",
        )["installation_id"])

    def test_dialog_cancellation_at_profile_and_confirmation_never_activates(self):
        self.install()
        with patch("ui.widgets.managed_packages_dialog.get_logger", return_value=Mock()):
            dialog = ManagedPackagesDialog(service=self.managed, activation_service=self.activation)
        self.addCleanup(dialog.deleteLater)
        dialog.reload()
        self.wait(dialog)
        dialog.table.selectRow(0)
        dialog._prepare_activation()
        self.wait(dialog)
        dialog.confirmation.done(QDialog.Rejected)
        self.assertIsNone(dialog.confirmation)
        self.assertEqual({}, self.active_snapshot())
        dialog._prepare_activation()
        self.wait(dialog)
        dialog.confirmation.done(QDialog.Accepted)
        self.wait(dialog)
        dialog.confirmation.done(QMessageBox.No)
        self.assertIsNone(dialog.confirmation)
        self.assertEqual({}, self.active_snapshot())

    def test_embedded_activation_blocks_other_input_nonblockingly_until_worker_completion(self):
        status = self.install(event=False)
        plan = self.prepare(status, "")
        with patch("ui.widgets.managed_packages_dialog.get_logger", return_value=Mock()):
            dialog = ManagedPackagesDialog(
                service=self.managed, activation_service=self.activation, embedded=True,
            )
        self.addCleanup(dialog.deleteLater)
        dialog._activation_prepared(plan)
        dialog.confirmation.done(QMessageBox.Yes)
        progress = dialog.activation_progress
        self.assertIsNotNone(progress)
        self.assertTrue(progress.isModal())
        self.assertTrue(progress.isVisible())
        progress.reject()
        self.assertTrue(progress.isVisible())
        self.wait(dialog)
        self.assertIsNone(dialog.activation_progress)
        self.assertTrue(self.active_snapshot())
        before = self.active_snapshot()
        with patch.object(self.activation, "activate", side_effect=PackageError("fixture activation failed")):
            dialog._activation_prepared(plan)
            dialog.confirmation.done(QMessageBox.Yes)
            self.assertIsNotNone(dialog.activation_progress)
            self.wait(dialog)
        self.assertIsNone(dialog.activation_progress)
        self.assertIn("fixture activation failed", dialog.status.text())
        self.assertEqual(before, self.active_snapshot())

    def test_active_update_ui_shows_versions_and_requires_default_no_consent_in_workers(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        self.function.name = "Fixed from upstream"
        self.newer_archive()
        threads, prepared_plans = [], []
        for name in ("active_status", "prepare_update", "update"):
            original = getattr(self.activation, name)
            def record(*args, _call=original, _name=name):
                threads.append(get_ident())
                result = _call(*args)
                if _name == "prepare_update":
                    prepared_plans.append(result)
                return result
            setattr(self.activation, name, Mock(side_effect=record))
        with patch("ui.widgets.managed_packages_dialog.get_logger", return_value=Mock()):
            dialog = ManagedPackagesDialog(
                service=self.managed, activation_service=self.activation, embedded=True,
            )
        self.addCleanup(dialog.deleteLater)
        dialog.reload()
        self.wait(dialog)
        dialog.table.selectRow(0)
        self.assertFalse(dialog.activate.isEnabled())
        self.assertTrue(dialog.active_update.isEnabled())
        self.assertEqual("1.1.0", dialog.table.item(0, 1).text())
        self.assertIn("1.0.0", dialog.table.item(0, 5).text())
        before = self.active_snapshot()
        dialog._prepare_active_update()
        self.wait(dialog)
        self.assertIn("1.0.0", dialog.details.toPlainText())
        self.assertIn("1.1.0", dialog.details.toPlainText())
        self.assertIn("technical-local", dialog.details.toPlainText())
        self.assertEqual(
            QMessageBox.No, dialog.confirmation.standardButton(dialog.confirmation.defaultButton()),
        )
        dialog.confirmation.done(QMessageBox.No)
        self.assertEqual(before, self.active_snapshot())
        self.activation.update.assert_not_called()
        dialog._prepare_active_update()
        self.wait(dialog)
        notifications = []
        bus = ReinitializeService()
        bus.subscribe(lambda source, payload: notifications.append((source, get_ident())))
        with patch("ui.widgets.managed_packages_dialog.get_default_reinitialize_service", return_value=bus):
            dialog.confirmation.done(QMessageBox.Yes)
            self.assertIsNotNone(dialog.activation_progress)
            self.wait(dialog)
        self.assertIsNone(dialog.activation_progress)
        self.activation.update.assert_called_once_with(status["installation_id"], prepared_plans[-1]["plan_sha256"])
        self.assertEqual(
            [(source, get_ident()) for source in ("library", "variable", "function", "scheduler")], notifications,
        )
        self.assertIn("1.1.0", dialog.table.item(0, 5).text())
        self.assertFalse(dialog.active_update.isEnabled())
        self.assertEqual(t("packages.active_update_complete"), dialog.status.text())
        self.assertNotIn(get_ident(), threads)
        self.assertFalse(read_json(self.root / "data/scheduler/game/event.json")["enabled"])
        before = self.active_snapshot()
        with patch.object(self.activation, "update", side_effect=PackageError("fixture update failed")):
            dialog._activation_prepared(prepared_plans[-1])
            dialog.confirmation.done(QMessageBox.Yes)
            self.assertIsNotNone(dialog.activation_progress)
            self.wait(dialog)
        self.assertIsNone(dialog.activation_progress)
        self.assertIn("fixture update failed", dialog.status.text())
        self.assertEqual(before, self.active_snapshot())

    def test_active_update_ui_rechecks_stale_clean_display_and_reports_corrupt_receipts(self):
        status = self.install()
        self.activate(status, self.prepare(status))
        self.newer_archive()
        with patch("ui.widgets.managed_packages_dialog.get_logger", return_value=Mock()):
            dialog = ManagedPackagesDialog(service=self.managed, activation_service=self.activation)
        self.addCleanup(dialog.deleteLater)
        dialog.reload()
        self.wait(dialog)
        dialog.table.selectRow(0)
        self.assertTrue(dialog.active_update.isEnabled())
        path = self.root / "data/functions/game/function.json"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        dialog._prepare_active_update()
        self.wait(dialog)
        self.assertIsNone(dialog.confirmation)
        self.assertIn("local active content changed", dialog.status.text())
        dialog.reload()
        self.wait(dialog)
        self.assertFalse(dialog.active_update.isEnabled())
        self.assertIn(t("packages.active_modified"), dialog.table.item(0, 5).text())
        self.assertIn("data/functions/game/function.json", dialog.table.item(0, 5).toolTip())
        receipt = self.activation._receipt_path(status["installation_id"])
        receipt.write_text("invalid")
        dialog.reload()
        self.wait(dialog)
        self.assertEqual(1, dialog.table.rowCount())
        self.assertEqual(t("packages.damaged"), dialog.table.item(0, 5).text())
        self.assertIn(t("packages.failed").split("{error}")[0], dialog.status.text())
        self.assertFalse(dialog.activate.isEnabled())
        self.assertFalse(dialog.active_update.isEnabled())
        self.assertTrue(dialog.open_original.isEnabled())

    def test_corrupt_receipt_structure_is_an_explicit_error_without_writes(self):
        status = self.install()
        self.assertEqual("not_active", self.activation.active_status(status["installation_id"])["state"])
        with self.assertRaisesRegex(PackageError, "not active"):
            self.activation.prepare_update(status["installation_id"])
        self.activate(status, self.prepare(status))
        receipt_path = self.activation._receipt_path(status["installation_id"])
        original = read_json(receipt_path)
        invalid = [
            {**original, "schema_version": []},
            {**original, "profile_id": None},
            {**original, "version": "not-a-version"},
            {**original, "variable_definitions": None},
            {**original, "active_files": {"../outside": "0" * 64}},
            {**original, "active_files": {"data/scheduler/other/event.json": "0" * 64}},
            {**original, "active_files": {"data/package-activations/other.json": "0" * 64}},
        ]
        for receipt in invalid:
            with self.subTest(receipt=receipt):
                atomic_write_json(receipt_path, receipt)
                before = self.active_snapshot()
                with self.assertRaises(PackageError):
                    self.activation.active_status(status["installation_id"])
                self.assertEqual(before, self.active_snapshot())

    def test_activation_texts_exist_in_both_supported_languages(self):
        self.addCleanup(set_language, translator.LANG)
        for language in ("en", "de"):
            set_language(language)
            for name in (
                "activate", "profile_required", "target_profile", "activation_summary",
                "activation_modified", "activation_rules", "activation_complete", "tab", "sources_hint",
                "active_update", "active_state", "active_not_active", "active_unchanged", "active_modified",
                "active_update_available", "active_update_summary", "active_update_rules", "active_update_complete",
            ):
                with self.subTest(language=language, name=name):
                    key = f"packages.{name}"
                    self.assertNotEqual(key, t(key))


if __name__ == "__main__":
    result = main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    raise SystemExit(0 if result.wasSuccessful() else 1)
