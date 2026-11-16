import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import installer as ins
import minecraft as mc


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "jogo"
        self.app = Path(self.temporary.name) / "app"
        self.web = {}
        self.requests = []
        self.request_patch = patch.object(ins, "request", side_effect=self.request)
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)

    def request(self, url):
        self.requests.append(url)
        if url not in self.web:
            raise OSError("Sem conexão: " + str(url))
        return io.BytesIO(self.web[url])

    def spec(self, url, data):
        if isinstance(data, dict):
            data = json.dumps(data).encode("utf-8")
        self.web[url] = data
        return {"url": url, "sha1": hashlib.sha1(data).hexdigest(), "size": len(data)}

    def fixture(self):
        native_bytes = io.BytesIO()
        with zipfile.ZipFile(native_bytes, "w") as output:
            output.writestr("example.dll", "native")
        native = self.spec("https://test/native", native_bytes.getvalue())
        native["path"] = "native/example.jar"
        library = self.spec("https://test/library", b"library")
        library["path"] = "group/example/1/example-1.jar"
        asset_data = b"asset bytes"
        digest = hashlib.sha1(asset_data).hexdigest()
        self.spec(ins.ASSET_URL + digest[:2] + "/" + digest, asset_data)
        asset = self.spec("https://test/assets", {"virtual": True, "objects": {
            "sound/example.ogg": {"hash": digest, "size": len(asset_data)},
            "sound/duplicate.ogg": {"hash": digest, "size": len(asset_data)},
        }})
        asset["id"] = "pre-1.6"
        logging = self.spec("https://test/logging", b"log config")
        logging["id"] = "client.xml"
        manifest = {
            "id": "1.0", "mainClass": "net.minecraft.launchwrapper.Launch",
            "minecraftArguments": "${auth_player_name} ${auth_session} --assetsDir ${game_assets}",
            "downloads": {"client": self.spec("https://test/client", b"client jar")},
            "javaVersion": {"component": "java-runtime-delta", "majorVersion": 21},
            "assetIndex": asset,
            "logging": {"client": {"file": logging, "argument": "-Dlog4j.configurationFile=${path}"}},
            "libraries": [{"name": "group:example:1", "downloads": {"artifact": library,
                              "classifiers": {"natives-test": native}},
                           "natives": {mc.OS_NAME: "natives-test"}},
                          {"name": "group:excluded:1", "rules": [{"action": "allow",
                           "os": {"name": "nonexistent"}}]}],
        }
        entry = {"id": "1.0", **self.spec("https://test/1.0", manifest)}
        other = {"id": "1.1", "url": "https://test/not-selected"}
        self.spec(ins.CATALOG_URL, {"latest": {"release": "1.1"}, "versions": [other, entry]})
        self.catalog = ins.parse_catalog(json.loads(self.web[ins.CATALOG_URL]))
        return manifest, digest

    def install(self, **kwargs):
        with patch.object(ins, "ensure_java", return_value=Path("java.exe")):
            ins.install_version("1.0", root=self.root, app_dir=self.app, catalog=self.catalog, **kwargs)

    def test_catalog_only_caches_metadata_and_includes_all_types(self):
        versions = [{"id": name, "type": kind, "url": "https://test/" + name}
                    for name, kind in [("1.0", "release"), ("snapshot", "snapshot"),
                                       ("alpha", "old_alpha"), ("beta", "old_beta")]]
        self.spec(ins.CATALOG_URL, {"versions": versions})
        result = ins.fetch_catalog(self.app)
        self.assertEqual(len(result.versions), 4)
        self.assertEqual(self.requests, [ins.CATALOG_URL])
        self.assertFalse(self.root.exists())
        self.assertTrue((self.app / "cache" / "versions.json").is_file())

    def test_catalog_falls_back_to_cache_without_network_when_requested(self):
        self.fixture()
        ins.fetch_catalog(self.app)
        self.requests.clear()
        self.assertTrue(ins.fetch_catalog(self.app, refresh=False).cached)
        self.assertFalse(self.requests)
        self.web.clear()
        self.assertTrue(ins.fetch_catalog(self.app).cached)

    def test_new_install_downloads_only_selected_version_and_reuses_every_file(self):
        _, digest = self.fixture()
        progress = []
        self.install(progress=lambda *args: progress.append(args))
        self.assertTrue((self.root / "versions" / "1.0" / "1.0.jar").is_file())
        self.assertFalse((self.root / "versions" / "1.1").exists())
        self.assertTrue((self.root / "libraries" / "native" / "example.jar").is_file())
        self.assertNotIn("https://test/not-selected", self.requests)
        self.assertEqual(self.requests.count(ins.ASSET_URL + digest[:2] + "/" + digest), 1)
        self.assertEqual(progress[-1][1], progress[-1][2])
        with patch.object(mc, "find_java", return_value=Path("java.exe")):
            plan = mc.build_plan("1.0", "Player", root=self.root, app_dir=self.app)
        self.assertIn("net.minecraft.launchwrapper.Launch", plan.command)
        self.assertIn("-Dlog4j.configurationFile=" + str(self.root / "assets" / "log_configs" / "client.xml"), plan.command)
        mc.prepare_files(plan)
        self.assertEqual((plan.natives_dir / "example.dll").read_text(), "native")
        self.assertEqual((self.root / "assets" / "virtual" / "pre-1.6" / "sound" / "example.ogg").read_bytes(), b"asset bytes")
        self.requests.clear()
        self.install()
        self.assertFalse(self.requests, "A segunda execução deve funcionar sem rede")

    def test_corrupted_files_are_repaired_using_sha1(self):
        self.fixture()
        self.install()
        jar = self.root / "versions" / "1.0" / "1.0.jar"
        jar.write_bytes(b"corrupted!")  # Mesmo tamanho: o SHA-1 precisa detectar.
        self.requests.clear()
        self.install()
        self.assertEqual(self.requests, ["https://test/client"])
        self.assertEqual(jar.read_bytes(), b"client jar")

    def test_failed_download_does_not_replace_existing_file_or_leave_part_files(self):
        path = Path(self.temporary.name) / "data.bin"
        path.write_bytes(b"original")
        spec = self.spec("https://test/data", b"wanted")
        self.web[spec["url"]] = b"wrong bytes"
        with patch.object(ins.time, "sleep"), self.assertRaisesRegex(mc.LaunchError, "Falha ao baixar"):
            ins.download_file(path, spec)
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(len(self.requests), 3)
        self.assertFalse(list(path.parent.glob("*.part")))

    def test_cancel_during_transfer_does_not_publish_partial_file(self):
        event = threading.Event()
        spec = self.spec("https://test/data", b"some bytes")
        path = Path(self.temporary.name) / "cancelled.bin"
        with self.assertRaises(ins.InstallCancelled):
            ins.download_file(path, spec, event, lambda *_: event.set())
        self.assertFalse(path.exists())
        self.assertFalse(list(path.parent.glob("*.part")))

    def test_cancel_before_install_creates_nothing(self):
        self.fixture()
        event = threading.Event()
        event.set()
        with self.assertRaises(ins.InstallCancelled):
            self.install(cancel=event)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.requests)

    def test_downloads_java_only_when_missing_and_marks_completed_runtime(self):
        manifest, _ = self.fixture()
        platform = ins.runtime_platform()
        exe_name = "java.exe" if mc.OS_NAME == "windows" else "java"
        runtime_manifest = {"files": {
            "release": {"type": "file", "downloads": {"raw": self.spec("https://test/release", b'JAVA_VERSION="21.0.7"')}},
            "bin/" + exe_name: {"type": "file", "executable": True,
                                "downloads": {"raw": self.spec("https://test/java", b"executable")}},
        }}
        runtime_spec = self.spec("https://test/runtime", runtime_manifest)
        self.spec(ins.RUNTIME_URL, {platform: {"java-runtime-delta": [{"manifest": runtime_spec}]}})
        with patch.object(ins, "find_java", side_effect=[mc.LaunchError("missing"), Path("java.exe")]):
            ins.ensure_java(manifest, self.root, {}, ins.Reporter(), None)
        directory = self.root / "runtime" / "blocklauncher" / "java-runtime-delta" / platform / "java-runtime-delta"
        self.assertTrue((directory / ".complete").is_file())
        self.assertTrue((directory / "bin" / exe_name).is_file())
        self.requests.clear()
        with patch.object(ins, "find_java", return_value=Path("java.exe")):
            ins.ensure_java(manifest, self.root, {}, ins.Reporter(), None)
        self.assertFalse(self.requests)

    def test_manual_java_error_does_not_trigger_an_unwanted_runtime_download(self):
        with patch.object(ins, "find_java", side_effect=mc.LaunchError("wrong Java")):
            with self.assertRaisesRegex(mc.LaunchError, "wrong Java"):
                ins.ensure_java({}, self.root, {"java_path": "manual.exe"}, ins.Reporter(), None)
        self.assertFalse(self.requests)

    def test_incomplete_managed_java_is_not_detected(self):
        exe_name = "java.exe" if mc.OS_NAME == "windows" else "java"
        directory = self.root / "runtime" / "blocklauncher" / "partial"
        java = directory / "bin" / exe_name
        java.parent.mkdir(parents=True)
        java.touch()
        (directory / "release").write_text('JAVA_VERSION="21.0.7"')
        with patch.object(mc.shutil, "which", return_value=None), patch.dict(os.environ, {
            "JAVA_HOME": "", "ProgramFiles": "", "ProgramFiles(x86)": "",
        }):
            with self.assertRaises(mc.LaunchError):
                mc.find_java(self.root, 21)
            (directory / ".complete").touch()
            self.assertEqual(mc.find_java(self.root, 21), java)

    def test_inherited_local_profile_downloads_missing_official_parent(self):
        self.fixture()
        child = self.root / "versions" / "my-profile" / "my-profile.json"
        child.parent.mkdir(parents=True)
        child.write_text(json.dumps({"id": "my-profile", "inheritsFrom": "1.0", "libraries": []}))
        with patch.object(ins, "ensure_java", return_value=Path("java.exe")):
            ins.install_version("my-profile", root=self.root, app_dir=self.app, catalog=self.catalog)
        self.assertTrue((self.root / "versions" / "1.0" / "1.0.jar").is_file())
        self.assertFalse((child.parent / "my-profile.jar").exists())


if __name__ == "__main__":
    unittest.main()
