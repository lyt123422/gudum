"""构建干净的 Windows 发布包：源码 + 干净 NapCat + WorkBuddy-Hub + 一键启动。

只读取 Git 跟踪的源码和当前 NapCat 运行时；不会把 ``.env``、``data/``、
``.venv/``、内部文档或 NapCat 账号配置带进发布包。输出在 ``dist/``（已忽略）。

用法：
    .venv\\Scripts\\python.exe tools\\build_release.py
    .venv\\Scripts\\python.exe tools\\build_release.py --version v1.0.0
    .venv\\Scripts\\python.exe tools\\build_release.py --workbuddy-zip path\\to\\release.zip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import workbuddy_setup  # noqa: E402

SOURCE_DIRS = ("app", "tests", "tools")
SOURCE_FILES = (
    "README.md",
    "TUTORIAL.md",
    "THIRD-PARTY.md",
    "LICENSE",
    "requirements.txt",
    ".env.example",
    ".gitignore",
    "start.bat",
    "start_napcat.bat",
    "控制台.bat",
    "一键启动.bat",
)
FORBIDDEN_NAMES = {
    ".env", "data", ".venv", ".git", "HANDOFF.md", "V3-REVIEW.md",
    "FACTS.md", "GUIDE-PAGE.md", "QA-CHECKLIST.md", "RECOVERY-CT.md",
}
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


def _source_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names
            if name in SKIP_DIRS or name.endswith(".pyc")}


def _copy_source(stage: Path) -> None:
    for dirname in SOURCE_DIRS:
        src = ROOT / dirname
        if not src.is_dir():
            raise SystemExit(f"缺少源码目录: {src}")
        shutil.copytree(src, stage / dirname, ignore=_source_ignore)
    for filename in SOURCE_FILES:
        src = ROOT / filename
        if not src.is_file():
            raise SystemExit(f"缺少发布文件: {src}")
        shutil.copy2(src, stage / filename)


def _napcat_ignore(directory: str, names: list[str]) -> set[str]:
    base = ROOT / "napcat"
    rel = Path(directory).resolve().relative_to(base.resolve()).as_posix()
    ignored: set[str] = set()
    if rel == ".":
        ignored.add("config")          # 根目录的 onebot11_<QQ>.json
    if rel == "napcat":
        # 内层 NapCat 运行目录：账号配置、WebUI 密码、二维码、运行日志
        ignored.update({"config", "cache", "logs"})
    for name in names:
        if name.startswith("guild1.db"):
            ignored.add(name)
        if name in {"beacon_report.log", "boot_test.log", "qrcode.png"}:
            ignored.add(name)
        if name.endswith(".log") and rel in {".", "napcat"}:
            ignored.add(name)
    return ignored


def _copy_napcat(stage: Path) -> None:
    src = ROOT / "napcat"
    if not (src / "node.exe").is_file() or not (src / "index.js").is_file():
        raise SystemExit("napcat/ 不完整：缺少 node.exe 或 index.js")
    shutil.copytree(src, stage / "napcat", ignore=_napcat_ignore)


def _copy_workbuddy(stage: Path, local_zip: Path | None) -> None:
    dest = stage / "vendor" / "WorkBuddy-Hub"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if local_zip is not None:
        if not local_zip.is_file():
            raise SystemExit(f"WorkBuddy-Hub zip 不存在: {local_zip}")
        digest = hashlib.sha256(local_zip.read_bytes()).hexdigest()
        workbuddy_setup._install_zip(local_zip, dest, digest, "", str(local_zip))
        return
    existing = ROOT / "vendor" / "WorkBuddy-Hub"
    if workbuddy_setup.is_install(existing):
        shutil.copytree(existing, dest,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return
    asset = workbuddy_setup._release_asset()
    downloaded: Path | None = None
    try:
        downloaded = workbuddy_setup._download(asset)
        workbuddy_setup._install_zip(
            downloaded, dest, asset["sha256"], asset["version"], asset["url"])
    finally:
        if downloaded:
            downloaded.unlink(missing_ok=True)


def _validate(stage: Path) -> None:
    for name in FORBIDDEN_NAMES:
        if (stage / name).exists():
            raise SystemExit(f"发布包包含禁止内容: {name}")
    for path in stage.rglob(".env"):
        raise SystemExit(f"发布包包含 .env: {path}")
    required = [
        "app/panel.py", "app/workbuddy_setup.py", "一键启动.bat",
        ".env.example", "napcat/node.exe", "napcat/index.js",
        "napcat/wrapper.node", "vendor/WorkBuddy-Hub/wb_proxy.py",
        "vendor/WorkBuddy-Hub/python/python.exe",
    ]
    for name in required:
        if not (stage / name).is_file():
            raise SystemExit(f"发布包缺少必要文件: {name}")
    for name in ("napcat/config", "napcat/napcat/config",
                 "napcat/napcat/cache", "napcat/napcat/logs",
                 "napcat/napcat/boot_test.log", "napcat/beacon_report.log"):
        if (stage / name).exists():
            raise SystemExit(f"发布包残留账号/运行数据: {name}")
    if list((stage / "napcat").glob("guild1.db*")):
        raise SystemExit("发布包残留 NapCat 账号数据库 guild1.db*")
    accounts = stage / "vendor" / "WorkBuddy-Hub" / "accounts"
    if accounts.exists() and any(accounts.glob("*.json")):
        raise SystemExit("WorkBuddy-Hub 发布包里残留 accounts/*.json")


def _manifest(stage: Path, version: str) -> None:
    data = {
        "name": "gudum",
        "version": version,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": "windows-x64",
        "components": {
            "napcat": "bundled clean runtime",
            "workbuddy_hub": "official release, bundled",
            "source": "gudum",
        },
        "notes": [
            "首次启动：双击 一键启动.bat，填写自己的模型 Key 和 QQ 小号。",
            "WorkBuddy：反代页点登录，登录自己的 WorkBuddy 账号。",
            "不要公开本目录下的 .env 和 data/。",
        ],
    }
    (stage / "RELEASE-MANIFEST.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _zip(stage: Path, zip_path: Path) -> None:
    total = sum(1 for p in stage.rglob("*") if p.is_file())
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                rel = path.relative_to(stage).as_posix()
                zf.write(path, f"{stage.name}/{rel}")
                count += 1
                if count % 500 == 0:
                    print(f"  打包 {count}/{total} 文件…")
    digest = hashlib.sha256()
    with zip_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    (zip_path.parent / (zip_path.name + ".sha256")).write_text(
        f"{digest.hexdigest()}  {zip_path.name}\n", encoding="utf-8")
    print(f"ZIP: {zip_path} ({zip_path.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"SHA256: {digest.hexdigest()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 gudum 发布包")
    parser.add_argument("--version", default="v1.0.0")
    parser.add_argument("--out", default="dist")
    parser.add_argument("--workbuddy-zip", type=Path, default=None)
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args(argv)

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    stage = out / f"gudum-{args.version}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    print(f"构建目录: {stage}")
    _copy_source(stage)
    print("  源码完成")
    _copy_napcat(stage)
    print("  NapCat 完成（已移除账号配置/二维码/日志/数据库）")
    _copy_workbuddy(stage, args.workbuddy_zip)
    print("  WorkBuddy-Hub 完成（官方发布包）")
    _manifest(stage, args.version)
    _validate(stage)
    print("  校验完成")

    if not args.no_zip:
        _zip(stage, out / f"gudum-{args.version}-windows-x64.zip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
