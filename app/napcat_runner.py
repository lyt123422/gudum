"""NapCat 启动壳：合并输出，逐行脱敏后显示；不修改协议端源码或配置。"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TextIO

from .log_setup import write_redacted

ROOT = Path(__file__).resolve().parents[1]


def relay_output(source: TextIO, destination: TextIO, log_file: TextIO | None = None) -> None:
    """只复制经过脱敏的文本；UTF-8 损坏由启动时的 errors=replace 兜底。"""
    for line in source:
        write_redacted(destination, line)
        if log_file is not None:
            write_redacted(log_file, line)


def node_command(napcat_dir: Path) -> list[str]:
    """协议端入口: Windows 用随包 node.exe; Linux 优先随包 node, 兜底系统 node。"""
    if sys.platform == "win32":
        return [str(napcat_dir / "node.exe"), "./index.js"]
    bundled = napcat_dir / "node"
    if bundled.exists():
        return [str(bundled), "./index.js"]
    system_node = shutil.which("node")
    if system_node:
        return [system_node, "./index.js"]
    raise FileNotFoundError("未找到 node: 安装 Node.js, 或把 NapCat 随包 node 放进 napcat/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default="")
    parser.add_argument("--log-file", type=Path)
    args = parser.parse_args(argv)
    napcat_dir = ROOT / "napcat"
    command = node_command(napcat_dir) + ["-q", args.account]
    # NapCat 官方支持 NAPCAT_QUICK_PASSWORD 环境变量做密码快速登录
    # （配置在 .env 的 NAPCAT_QUICK_PASSWORD / ACCOUNT），免扫码
    from .envutil import load_env
    child_env = dict(os.environ)
    for key in ("NAPCAT_QUICK_PASSWORD", "NAPCAT_QUICK_PASSWORD_MD5", "ACCOUNT"):
        value = load_env(ROOT / ".env").get(key, "")
        if value:
            child_env[key] = value
    output = args.log_file.open("a", encoding="utf-8") if args.log_file else None
    try:
        # 不使用 shell，账号不会被解释为命令；返回协议端退出码。
        with subprocess.Popen(command, cwd=napcat_dir, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                              errors="replace", env=child_env,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            assert process.stdout is not None
            try:
                relay_output(process.stdout, sys.stdout, output)
            finally:
                process.stdout.close()
            return process.wait()
    finally:
        if output is not None:
            output.close()


if __name__ == "__main__":
    raise SystemExit(main())
