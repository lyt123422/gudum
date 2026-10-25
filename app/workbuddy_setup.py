"""WorkBuddy-Hub 本机安装/定位。

gudum 不把 WorkBuddy-Hub 的 10 MB 发布包塞进仓库：首次点「登录 WorkBuddy」
时从官方 GitHub Release 下载、校验 SHA256、解压到 ``data/workbuddy-hub/``；
之后每个用户都在自己电脑上登录自己的 WorkBuddy 账号，不经过任何人的端点。

支持三种来源（优先级从高到低）：
1. ``WORKBUDDY_HUB_DIR``：已有安装目录（含 ``wb_proxy.py``）；
2. ``WORKBUDDY_HUB_ZIP`` 或 ``data/workbuddy-hub.zip`` / 项目根目录的
   ``workbuddy2api-hub.zip``：离线包；
3. 官方 Release 下载（默认固定版本，可用 ``WORKBUDDY_HUB_VERSION=latest``
   或 ``WORKBUDDY_HUB_URL`` + ``WORKBUDDY_HUB_SHA256`` 覆盖）。

本模块只用标准库，面板和内核都可以安全导入。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
INSTALL_DIR = ROOT / "data" / "workbuddy-hub"
META_NAME = ".gudum-workbuddy.json"

REPO = "ardeyouxipianyi/workbuddy2api-hub"
DEFAULT_VERSION = "v1.8.0"
DEFAULT_ZIP = f"workbuddy2api-hub-{DEFAULT_VERSION}.zip"
DEFAULT_URL = f"https://github.com/{REPO}/releases/download/{DEFAULT_VERSION}/{DEFAULT_ZIP}"
DEFAULT_SHA256 = "0e3b94fd6628f29944f9a514882a5f9445cfdf320a818a738dbe08bec343c8af"

MAX_ZIP_BYTES = 80 * 1024 * 1024
MAX_UNPACKED_BYTES = 400 * 1024 * 1024
_USER_AGENT = "gudum-workbuddy-setup/1.0"
_lock = threading.Lock()
_version_cache: dict[tuple[str, float], str] = {}


class SetupError(RuntimeError):
    """安装/下载/解压失败；消息直接给面板展示。"""


def install_dir() -> Path:
    raw = (os.environ.get("WORKBUDDY_HUB_DIR") or "").strip()
    return Path(raw).expanduser() if raw else INSTALL_DIR


def is_install(path: Path | str | None) -> bool:
    """目录是否像一个可运行的 WorkBuddy-Hub 安装。"""
    if not path:
        return False
    p = Path(path).expanduser()
    return (p / "wb_proxy.py").is_file() and (p / "wb_accounts.py").is_file()


def _install_root(path: Path) -> Path | None:
    """兼容用户选到父目录（里面还有 wb-proxy/）的情况。"""
    for candidate in (path, path / "wb-proxy", path / "workbuddy2api-hub"):
        if is_install(candidate):
            return candidate
    return None


def _candidate_dirs(preferred: Path | str | None) -> list[Path]:
    out: list[Path] = []

    def add(value: Path | str | None) -> None:
        if not value:
            return
        try:
            p = Path(value).expanduser()
        except (OSError, ValueError):
            return
        if p not in out:
            out.append(p)

    add(os.environ.get("WORKBUDDY_HUB_DIR"))
    add(preferred)
    add(INSTALL_DIR)
    add(ROOT / "WorkBuddy-Hub")
    add(ROOT / "vendor" / "WorkBuddy-Hub")
    add(ROOT.parent / "WorkBuddy-Hub")
    add(Path.home() / "WorkBuddy-Hub")
    add(Path.home() / "Desktop" / "WorkBuddy-Hub")
    if os.name == "nt":
        for drive in "DEFGH":
            add(f"{drive}:/WorkBuddy-Hub")
            add(f"{drive}:/workbuddy2api-hub")
    return out


def detect(preferred: Path | str | None = None) -> Path | None:
    """返回第一个有效安装目录；没有则 None。"""
    for candidate in _candidate_dirs(preferred):
        root = _install_root(candidate)
        if root:
            return root
    return None


def locate(value: str) -> str | None:
    """校验用户输入的具体目录，返回真正的安装根（供面板线程调用）。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    root = _install_root(Path(raw).expanduser())
    return str(root) if root else None


def _same_path(a: Path | str | None, b: Path | str | None) -> bool:
    if not a or not b:
        return False
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except (OSError, RuntimeError):
        return False


def _version(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (str(path), st.st_mtime)
    cached = _version_cache.get(key)
    if cached is not None:
        return cached
    version = ""
    for meta_path in (path / META_NAME, path / "RELEASE-MANIFEST.json",
                      path.parent / "release-manifest.json"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                version = str(data.get("version") or data.get("tag") or "")
                if version:
                    break
        except (OSError, ValueError):
            continue
    if not version:
        try:
            text = (path / "wb_proxy.py").read_text(encoding="utf-8", errors="replace")
            match = re.search(r'server_version\s*=\s*["\']wb-proxy/([^"\']+)', text)
            if match:
                version = match.group(1)
        except OSError:
            pass
    if len(_version_cache) > 64:
        _version_cache.clear()
    _version_cache[key] = version
    return version


def _status_payload(path: Path, installed: bool, source: str) -> dict:
    return {
        "installed": installed,
        "path": str(path),
        "install_dir": str(install_dir()),
        "version": _version(path) if installed else "",
        "source": source,
        "bundled_python": bool(installed and (path / "python" / "python.exe").is_file()),
        "default_version": DEFAULT_VERSION,
    }


def status(preferred: Path | str | None = None) -> dict:
    """给面板用的安装状态；不触发下载。"""
    found = detect(preferred)
    path = found or (Path(preferred).expanduser() if preferred else install_dir())
    if found and _same_path(path, os.environ.get("WORKBUDDY_HUB_DIR")):
        source = "环境变量"
    elif found and _same_path(path, preferred):
        source = "已配置路径"
    elif found and _same_path(path, install_dir()):
        source = "gudum 安装"
    elif found:
        source = "本机已有"
    else:
        source = "未安装"
    return _status_payload(path, bool(found), source)


def _local_zip() -> Path | None:
    raw = (os.environ.get("WORKBUDDY_HUB_ZIP") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_file():
            raise SetupError(f"WORKBUDDY_HUB_ZIP 指向的文件不存在：{p}")
        return p
    for candidate in (install_dir().with_suffix(".zip"),
                      INSTALL_DIR.with_suffix(".zip"),
                      ROOT / "workbuddy2api-hub.zip"):
        if candidate.is_file():
            return candidate
    return None


def _http_open(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    return urllib.request.urlopen(req, timeout=60)   # noqa: S310 - 固定官方/用户指定 URL


def _read_limited(response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise SetupError(f"下载内容超过限制（{limit // 1024 // 1024} MB）")
        chunks.append(chunk)
    return b"".join(chunks)


def _get_bytes(url: str, limit: int = 1024 * 1024) -> bytes:
    try:
        with _http_open(url) as response:
            return _read_limited(response, limit)
    except SetupError:
        raise
    except Exception as exc:   # noqa: BLE001 - 统一翻译成面板可读错误
        raise SetupError(f"下载失败：{url}（{str(exc)[:160]}）") from exc


def _get_json(url: str) -> dict:
    raw = _get_bytes(url)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SetupError(f"GitHub 返回的不是 JSON：{url}") from exc
    if not isinstance(data, dict):
        raise SetupError(f"GitHub 返回格式异常：{url}")
    return data


def _parse_sha256(text: str, filename: str) -> str:
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename:
            value = parts[0].lower()
            if re.fullmatch(r"[0-9a-f]{64}", value):
                return value
    return ""


def _release_asset(version: str | None = None) -> dict:
    """返回 {"url", "sha256", "version"}；校验值来自官方 SHA256SUMS。"""
    custom_url = (os.environ.get("WORKBUDDY_HUB_URL") or "").strip()
    if custom_url:
        sha = (os.environ.get("WORKBUDDY_HUB_SHA256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise SetupError("使用 WORKBUDDY_HUB_URL 时必须同时提供 64 位 WORKBUDDY_HUB_SHA256")
        return {"url": custom_url, "sha256": sha, "version": "custom"}

    version = (version or os.environ.get("WORKBUDDY_HUB_VERSION")
               or DEFAULT_VERSION).strip()
    if version == DEFAULT_VERSION:
        return {"url": DEFAULT_URL, "sha256": DEFAULT_SHA256, "version": version}

    api = (f"https://api.github.com/repos/{REPO}/releases/latest"
           if version == "latest"
           else f"https://api.github.com/repos/{REPO}/releases/tags/{version}")
    data = _get_json(api)
    tag = str(data.get("tag_name") or version)
    assets = {
        str(item.get("name") or ""): str(item.get("browser_download_url") or "")
        for item in data.get("assets") or []
        if isinstance(item, dict)
    }
    zip_name = next((name for name in assets
                     if name.endswith(".zip") and "workbuddy2api-hub" in name), "")
    if not zip_name or not assets.get(zip_name):
        raise SetupError(f"Release {tag} 里没有 workbuddy2api-hub 的 zip 包")
    sums_url = assets.get("SHA256SUMS") or assets.get("checksums.txt") or ""
    if not sums_url:
        raise SetupError(f"Release {tag} 里没有校验文件，拒绝未校验安装")
    sums = _get_bytes(sums_url).decode("utf-8", errors="replace")
    sha = _parse_sha256(sums, zip_name)
    if not sha:
        raise SetupError(f"校验文件里找不到 {zip_name} 的 SHA256")
    return {"url": assets[zip_name], "sha256": sha, "version": tag}


def _download(asset: dict) -> Path:
    tmp_dir = install_dir().parent
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".workbuddy-download-", suffix=".zip",
                                dir=str(tmp_dir))
    path = Path(name)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            try:
                with _http_open(asset["url"]) as response:
                    length = int(response.headers.get("Content-Length") or 0)
                    if length > MAX_ZIP_BYTES:
                        raise SetupError(f"发布包超过大小限制：{length} 字节")
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_ZIP_BYTES:
                            raise SetupError("发布包超过大小限制")
                        out.write(chunk)
                        digest.update(chunk)
            except SetupError:
                raise
            except Exception as exc:   # noqa: BLE001
                raise SetupError(f"下载 WorkBuddy-Hub 失败：{str(exc)[:200]}") from exc
        expected = str(asset.get("sha256") or "").lower()
        if expected and digest.hexdigest() != expected:
            raise SetupError("WorkBuddy-Hub 发布包 SHA256 校验失败，已丢弃下载文件")
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _safe_member(info: zipfile.ZipInfo) -> str:
    name = info.filename.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise SetupError(f"压缩包包含绝对路径，已拒绝：{info.filename}")
    parts = PurePosixPath(name).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise SetupError(f"压缩包包含非法路径，已拒绝：{info.filename}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise SetupError(f"压缩包包含符号链接，已拒绝：{info.filename}")
    return name


def _extract_release(zip_path: Path, dest: Path) -> None:
    """只解压 ``wb-proxy/`` 下的文件到 dest，防 zip-slip / 符号链接。"""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise SetupError(f"不是有效的 zip 包：{zip_path}") from exc
    with zf:
        infos = zf.infolist()
        roots = {_safe_member(info).split("/", 1)[0] for info in infos}
        prefix = "wb-proxy" if "wb-proxy" in roots else ""
        unpacked = 0
        for info in infos:
            name = _safe_member(info)
            if prefix:
                if name == prefix:
                    continue
                if not name.startswith(prefix + "/"):
                    continue
                rel = name[len(prefix) + 1:]
            else:
                rel = name
            if not rel:
                continue
            target = dest / PurePosixPath(rel)
            try:
                target.resolve().relative_to(dest.resolve())
            except ValueError as exc:
                raise SetupError(f"压缩包路径越界，已拒绝：{name}") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            unpacked += info.file_size
            if unpacked > MAX_UNPACKED_BYTES:
                raise SetupError("解压后体积超过限制，已拒绝")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            mode = (info.external_attr >> 16) & 0o777
            if mode and os.name != "nt":
                try:
                    target.chmod(mode)
                except OSError:
                    pass
        # 发布包根目录的 manifest 对排查版本有用，顺手留一份。
        for info in infos:
            if _safe_member(info) == "release-manifest.json":
                target = dest / "RELEASE-MANIFEST.json"
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
                break
    if not is_install(dest):
        raise SetupError("发布包里没有找到 wb_proxy.py / wb_accounts.py")


def _install_zip(zip_path: Path, dest: Path, expected_sha: str,
                 version: str, source: str) -> None:
    if expected_sha:
        digest = hashlib.sha256()
        with zip_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha.lower():
            raise SetupError("WorkBuddy-Hub 安装包 SHA256 校验失败")
    if dest.exists():
        if is_install(dest):
            return
        if any(dest.iterdir()):
            raise SetupError(
                f"{dest} 已存在但不是有效 WorkBuddy-Hub 安装；"
                "请换一个路径或先手动清理该目录")
        dest.rmdir()
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".workbuddy-install-", dir=str(dest.parent)))
    try:
        _extract_release(zip_path, staging)
        meta = {
            "version": version,
            "source": source,
            "sha256": expected_sha,
            "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "repo": f"https://github.com/{REPO}",
        }
        (staging / META_NAME).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staging, dest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def ensure_installed(preferred: Path | str | None = None) -> dict:
    """确保本机有可运行的 WorkBuddy-Hub，返回安装信息。

    已有安装直接复用；没有则从官方 Release 下载（或使用离线 zip）。
    这个函数会阻塞下载，面板应放到线程里调用。
    """
    with _lock:
        found = detect(preferred)
        if found:
            return {**status(found), "already": True}

        dest = install_dir()
        if dest.exists() and not is_install(dest):
            if any(dest.iterdir()):
                raise SetupError(
                    f"{dest} 已存在但不是有效 WorkBuddy-Hub 安装；"
                    "请在反代页「高级」里指定已有目录，或手动清理后重试")
            dest.rmdir()

        local = _local_zip()
        if local:
            expected = (os.environ.get("WORKBUDDY_HUB_SHA256") or "").strip().lower()
            _install_zip(local, dest, expected, "", str(local))
            return {**_status_payload(dest, True, "离线包"), "already": False}

        asset = _release_asset()
        downloaded: Path | None = None
        try:
            downloaded = _download(asset)
            _install_zip(downloaded, dest, asset["sha256"],
                         asset["version"], asset["url"])
        finally:
            if downloaded:
                downloaded.unlink(missing_ok=True)
        return {**_status_payload(dest, True, "官方 Release"), "already": False}
