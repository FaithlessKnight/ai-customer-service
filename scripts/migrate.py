#!/usr/bin/env python
"""
数据库迁移管理脚本

用法:
    python scripts/migrate.py upgrade          # 升级到最新版本
    python scripts/migrate.py upgrade 002      # 升级到指定版本
    python scripts/migrate.py downgrade -1     # 回滚一个版本
    python scripts/migrate.py downgrade base   # 回滚到初始状态
    python scripts/migrate.py current          # 查看当前版本
    python scripts/migrate.py history          # 查看迁移历史
    python scripts/migrate.py revision "描述"  # 生成新迁移文件
    python scripts/migrate.py check            # 检查是否有未执行的迁移

注意:
    - 生产环境执行前请先备份数据库
    - 所有 DDL 操作在事务内执行，失败自动回滚
"""
from __future__ import annotations

import os
import sys
import subprocess
import argparse
import logging

# 确保从项目根目录执行
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def run_alembic(*args: str) -> int:
    """执行 alembic 子命令，返回退出码。"""
    cmd = [sys.executable, "-m", "alembic"] + list(args)
    logger.info("执行: %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    return result.returncode


def cmd_upgrade(target: str = "head") -> None:
    """升级数据库到指定版本（默认最新）。"""
    logger.info("== 开始升级数据库迁移 (目标: %s) ==", target)
    code = run_alembic("upgrade", target)
    if code == 0:
        logger.info("== 迁移升级完成 ==")
    else:
        logger.error("== 迁移升级失败，请检查日志 ==")
        sys.exit(code)


def cmd_downgrade(target: str = "-1") -> None:
    """回滚数据库到指定版本。"""
    logger.warning("== 开始回滚数据库迁移 (目标: %s) ==", target)
    confirm = input(f"确认回滚到 {target}？此操作可能导致数据丢失 [y/N]: ")
    if confirm.lower() != "y":
        logger.info("已取消")
        return
    code = run_alembic("downgrade", target)
    if code == 0:
        logger.info("== 迁移回滚完成 ==")
    else:
        logger.error("== 迁移回滚失败 ==")
        sys.exit(code)


def cmd_current() -> None:
    """查看数据库当前迁移版本。"""
    run_alembic("current")


def cmd_history() -> None:
    """查看迁移历史记录。"""
    run_alembic("history", "--verbose")


def cmd_revision(message: str) -> None:
    """生成新的迁移脚本文件。"""
    if not message:
        logger.error("请提供迁移描述，例如: python scripts/migrate.py revision \"add user_id column\"")
        sys.exit(1)
    code = run_alembic("revision", "--message", message)
    if code == 0:
        logger.info("迁移文件已生成，请编辑 migrations/versions/ 中的新文件")
    else:
        sys.exit(code)


def cmd_check() -> None:
    """检查是否有未执行的迁移（CI/CD 用）。"""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=ROOT, capture_output=True, text=True
    )
    if "(head)" in result.stdout:
        logger.info("数据库已是最新版本")
    else:
        logger.warning("存在未执行的迁移，请运行: python scripts/migrate.py upgrade")
        logger.info("当前状态: %s", result.stdout.strip())
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="数据库迁移管理工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # upgrade
    p_up = subparsers.add_parser("upgrade", help="升级迁移")
    p_up.add_argument("target", nargs="?", default="head", help="目标版本（默认 head）")

    # downgrade
    p_down = subparsers.add_parser("downgrade", help="回滚迁移")
    p_down.add_argument("target", nargs="?", default="-1", help="目标版本（默认 -1）")

    # current / history / check
    subparsers.add_parser("current",  help="查看当前版本")
    subparsers.add_parser("history",  help="查看迁移历史")
    subparsers.add_parser("check",    help="检查是否有未执行迁移（CI 用）")

    # revision
    p_rev = subparsers.add_parser("revision", help="生成新迁移文件")
    p_rev.add_argument("message", help="迁移描述")

    args = parser.parse_args()

    if args.command == "upgrade":
        cmd_upgrade(args.target)
    elif args.command == "downgrade":
        cmd_downgrade(args.target)
    elif args.command == "current":
        cmd_current()
    elif args.command == "history":
        cmd_history()
    elif args.command == "revision":
        cmd_revision(args.message)
    elif args.command == "check":
        cmd_check()


if __name__ == "__main__":
    main()
