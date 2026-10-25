"""WorkBuddy-Hub 安装链路测试：不联网、不启动真实反代。"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app import workbuddy_setup as setup


def _make_zip(path: Path, *, slip: bool = False) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("wb-proxy/wb_proxy.py", 'server_version = "wb-proxy/9.9.9"\n')
        zf.writestr("wb-proxy/wb_accounts.py", "VALUE = 1\n")
        zf.writestr("wb-proxy/LICENSE", "MIT\n")
        zf.writestr("release-manifest.json", '{"files": []}\n')
        if slip:
            zf.writestr("wb-proxy/../evil.txt", "x")
    return path


class TestLocate(unittest.TestCase):
    def test_direct_and_parent_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "wb-proxy"
            root.mkdir()
            (root / "wb_proxy.py").write_text("x", encoding="utf-8")
            (root / "wb_accounts.py").write_text("x", encoding="utf-8")
            self.assertTrue(setup.is_install(root))
            self.assertEqual(setup.locate(str(root)), str(root))
            self.assertEqual(setup.locate(tmp), str(root))
            self.assertIsNone(setup.locate(str(Path(tmp) / "nope")))

    def test_status_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "missing"
            with mock.patch.object(setup, "detect", return_value=None), \
                    mock.patch.object(setup, "install_dir", return_value=target):
                info = setup.status()
            self.assertFalse(info["installed"])
            self.assertEqual(info["path"], str(target))
            self.assertEqual(info["source"], "未安装")


class TestInstallZip(unittest.TestCase):
    def test_pinned_release_has_checksum(self):
        with mock.patch.dict("os.environ", {
            "WORKBUDDY_HUB_URL": "",
            "WORKBUDDY_HUB_VERSION": "",
        }, clear=False):
            asset = setup._release_asset()
        self.assertIn("workbuddy2api-hub-v1.8.0.zip", asset["url"])
        self.assertEqual(len(asset["sha256"]), 64)

    def test_installs_prefix_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = _make_zip(tmp_path / "wb.zip")
            dest = tmp_path / "installed"
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            setup._install_zip(archive, dest, digest, "v9.9.9", "test")
            self.assertTrue(setup.is_install(dest))
            self.assertEqual(setup._version(dest), "v9.9.9")
            self.assertTrue((dest / "LICENSE").is_file())
            self.assertTrue((dest / "RELEASE-MANIFEST.json").is_file())

    def test_rejects_zip_slip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = _make_zip(tmp_path / "bad.zip", slip=True)
            dest = tmp_path / "installed"
            with self.assertRaises(setup.SetupError):
                setup._install_zip(archive, dest, "", "", "test")
            self.assertFalse(dest.exists())

    def test_rejects_checksum_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = _make_zip(tmp_path / "wb.zip")
            with self.assertRaises(setup.SetupError):
                setup._install_zip(archive, tmp_path / "installed", "0" * 64,
                                   "", "test")
            self.assertFalse((tmp_path / "installed").exists())

    def test_refuses_nonempty_invalid_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dest = tmp_path / "installed"
            dest.mkdir()
            (dest / "keep.txt").write_text("user data", encoding="utf-8")
            archive = _make_zip(tmp_path / "wb.zip")
            with self.assertRaises(setup.SetupError):
                setup._install_zip(archive, dest, "", "", "test")
            self.assertTrue((dest / "keep.txt").is_file())


class TestEnsureInstalled(unittest.TestCase):
    def test_uses_local_zip_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = _make_zip(tmp_path / "offline.zip")
            dest = tmp_path / "installed"
            with mock.patch.object(setup, "INSTALL_DIR", dest), \
                    mock.patch.object(setup, "detect", return_value=None), \
                    mock.patch.dict("os.environ", {
                        "WORKBUDDY_HUB_ZIP": str(archive),
                        "WORKBUDDY_HUB_DIR": "",
                    }, clear=False):
                info = setup.ensure_installed()
            self.assertTrue(info["installed"])
            self.assertFalse(info["already"])
            self.assertEqual(info["version"], "9.9.9")
            self.assertTrue(info["bundled_python"] is False)


class TestPanelInstallEndpoint(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from app import panel
        patches = [
            mock.patch.object(panel.proxy_manager, "config",
                              lambda: {"path": "C:/old", "port": 8788}),
            mock.patch.object(panel.proxy_manager, "save_config", lambda cfg: None),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return TestClient(panel.app, base_url="http://127.0.0.1:8090"), panel

    def test_install_success(self):
        client, panel = self._client()
        info = {"installed": True, "path": "C:/wb", "version": "1.8.0",
                "source": "官方 Release", "already": False}
        with mock.patch.object(panel.workbuddy_setup, "ensure_installed",
                               return_value=info):
            data = client.post("/api/proxy/install", json={}).json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["path"], "C:/wb")
        self.assertEqual(data["version"], "1.8.0")

    def test_install_error_is_reported(self):
        client, panel = self._client()
        with mock.patch.object(panel.workbuddy_setup, "ensure_installed",
                               side_effect=panel.workbuddy_setup.SetupError("下载失败")), \
                mock.patch.object(panel.workbuddy_setup, "status",
                                  return_value={"installed": False}):
            data = client.post("/api/proxy/install", json={}).json()
        self.assertFalse(data["ok"])
        self.assertIn("下载失败", data["error"])


if __name__ == "__main__":
    unittest.main()
