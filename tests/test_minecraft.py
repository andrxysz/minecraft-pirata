import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

import minecraft as mc


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Minecraft com espaços"
        self.app = Path(self.temporary.name) / "launcher"
        self.root.mkdir()

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def install(self, version="base", **overrides):
        data = {
            "id": version, "mainClass": "example.Main", "assets": "test",
            "javaVersion": {"majorVersion": 21}, "libraries": [],
            "arguments": {"jvm": ["-cp", "${classpath}"],
                          "game": ["--username", "${auth_player_name}", "--gameDir", "${game_directory}"]},
        }
        data.update(overrides)
        directory = self.root / "versions" / version
        self.write_json(directory / (version + ".json"), data)
        (directory / (version + ".jar")).touch()
        self.write_json(self.root / "assets" / "indexes" / "test.json", {"objects": {}})
        return data

    def plan(self, version="base", nickname="Player", settings=None):
        with patch.object(mc, "find_java", return_value=Path("java.exe")):
            return mc.build_plan(version, nickname, settings, self.root, self.app)

    def test_offline_uuid_matches_java_name_uuid(self):
        self.assertEqual(str(uuid.UUID(mc.offline_uuid("Notch"))), "b50ad385-829d-3141-a216-7e7d7539ba7f")
        self.assertNotEqual(mc.offline_uuid("Player"), mc.offline_uuid("player"))

    def test_settings_preserve_optional_fields_and_invalid_file_recovers(self):
        mc.save_settings({"nickname": "Teste", "memory_mb": 1024}, self.app)
        self.assertEqual(mc.load_settings(self.app)["memory_mb"], 1024)
        (self.app / "settings.json").write_text("broken", encoding="utf-8")
        self.assertEqual(mc.load_settings(self.app), {})

    def test_build_preserves_spaces_and_does_not_write_files(self):
        self.install()
        plan = self.plan()
        self.assertIn(str(self.root.resolve()), plan.command)
        self.assertIn("-Xmx2048M", plan.command)
        self.assertFalse(self.app.exists())

    def test_nickname_and_memory_validation(self):
        self.install()
        for nickname in ("ab", "a b", "a;calc", "ábc", "a" * 17):
            with self.subTest(nickname=nickname), self.assertRaises(mc.LaunchError):
                self.plan(nickname=nickname)
        for memory in (0, 511, "bad", 20000):
            with self.subTest(memory=memory), self.assertRaises(mc.LaunchError):
                self.plan(settings={"memory_mb": memory})

    def test_inherited_libraries_override_parent_and_keep_client(self):
        self.install(libraries=[{"name": "group:lib:1"}])
        self.write_json(self.root / "versions" / "mod" / "mod.json", {
            "id": "mod", "inheritsFrom": "base", "mainClass": "mod.Main",
            "libraries": [{"name": "group:lib:2"}],
            "arguments": {"game": ["--mod", "yes"]},
        })
        lib = self.root / "libraries" / mc.maven_path("group:lib:2")
        lib.parent.mkdir(parents=True)
        lib.touch()
        plan = self.plan("mod")
        self.assertIn("mod.Main", plan.command)
        self.assertIn("--username", plan.command)
        self.assertEqual(plan.command[-2:], ["--mod", "yes"])
        cp = plan.command[plan.command.index("-cp") + 1]
        self.assertIn("lib-2.jar", cp)
        self.assertNotIn("lib-1.jar", cp)
        self.assertIn("base.jar", cp)

    def test_legacy_arguments_are_expanded_after_tokenizing(self):
        self.install(arguments={}, minecraftArguments="--username ${auth_player_name} --gameDir ${game_directory}")
        plan = self.plan()
        self.assertEqual(plan.command[-1], str(self.root.resolve()))
        self.assertIn("-Djava.library.path=" + str(plan.natives_dir), plan.command)

    def test_rules_exclude_other_os_and_optional_features(self):
        with patch.object(mc, "OS_NAME", "windows"), patch.object(mc, "ARCH", "x86_64"):
            self.assertTrue(mc.rules_allow({}))
            self.assertFalse(mc.rules_allow({"rules": [{"action": "allow", "os": {"name": "osx"}}]}))
            self.assertFalse(mc.rules_allow({"rules": [{"action": "allow", "features": {"is_demo_user": True}}]}))
            self.assertTrue(mc.rules_allow({"rules": [{"action": "allow", "features": {"is_demo_user": False}}]}))
            self.assertFalse(mc.rules_allow({"rules": [{"action": "allow"}, {"action": "disallow", "os": {"name": "windows"}}]}))
            self.assertTrue(mc.rules_allow({"rules": [{"action": "allow", "os": {"arch": "amd64"}}]}))

    def test_inheritance_cycle_has_clear_error(self):
        self.install(inheritsFrom="other")
        self.install("other", inheritsFrom="base")
        with self.assertRaisesRegex(mc.LaunchError, "circular"):
            mc.resolve_version(self.root, "base")

    def test_missing_library_and_client_have_clear_error(self):
        self.install(libraries=[{"name": "group:missing:1"}])
        with self.assertRaisesRegex(mc.LaunchError, "missing-1.jar"):
            self.plan()
        self.install()
        (self.root / "versions" / "base" / "base.jar").unlink()
        with self.assertRaisesRegex(mc.LaunchError, "arquivo do jogo"):
            self.plan()

    def test_rejects_path_escape_in_versions_libraries_and_natives(self):
        with self.assertRaises(mc.LaunchError):
            mc.resolve_version(self.root, "../other")
        self.install(libraries=[{"name": "g:a:1", "downloads": {"artifact": {"path": "../../outside.jar"}}}])
        with self.assertRaises(mc.LaunchError):
            self.plan()
        self.install()
        archive = self.root / "bad.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("../outside.dll", "bad")
        plan = self.plan()
        plan.natives = [(archive, [])]
        with self.assertRaises(mc.LaunchError):
            mc.prepare_files(plan)
        self.assertFalse((plan.natives_dir.parent / "outside.dll").exists())

    def test_extracts_native_excluding_metadata(self):
        self.install()
        archive = self.root / "native.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("META-INF/MANIFEST.MF", "ignored")
            output.writestr("lwjgl.dll", "native")
        plan = self.plan()
        plan.natives = [(archive, ["META-INF/"])]
        mc.prepare_files(plan)
        self.assertEqual((plan.natives_dir / "lwjgl.dll").read_text(), "native")
        self.assertFalse((plan.natives_dir / "META-INF").exists())

    def test_unknown_placeholder_is_reported(self):
        self.install(arguments={"game": ["${unknown_placeholder}"]})
        with self.assertRaisesRegex(mc.LaunchError, "unknown_placeholder"):
            self.plan()

    def test_java_detection_reads_runtime_release_and_selects_exact_major(self):
        exe = "java.exe" if mc.OS_NAME == "windows" else "java"
        for major, version in ((8, "1.8.0_51"), (21, "21.0.7")):
            java = self.root / "runtime" / str(major) / "bin" / exe
            java.parent.mkdir(parents=True)
            java.touch()
            (java.parent.parent / "release").write_text(f'JAVA_VERSION="{version}"')
        selected = mc.find_java(self.root, 8)
        self.assertEqual(selected.parent.parent.name, "8")
        with self.assertRaisesRegex(mc.LaunchError, "Java 21"):
            mc.find_java(self.root, 21, str(selected))

    def test_argfile_is_understood_by_real_java_without_starting_minecraft(self):
        java = shutil.which("java")
        if not java or (mc.java_major(Path(java)) or 0) < 9:
            self.skipTest("Java 9+ não disponível para validar arquivo de argumentos")
        plan = mc.LaunchPlan([java, '-Dblock.test=Espaços \\ caminho "entre aspas"',
                              "-XshowSettings:properties", "-version"], self.root,
                             self.app / "natives", [], mc.java_major(Path(java)), "smoke-test")
        process = mc.start_game(plan, self.app)
        self.assertEqual(process.wait(timeout=15), 0)
        content = (self.app / "logs" / "game.log").read_text(encoding="utf-8", errors="replace")
        self.assertIn('\\ caminho "entre aspas"', content)
        self.assertIn("java.version", content)


if __name__ == "__main__":
    unittest.main()
