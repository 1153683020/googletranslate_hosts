#!/usr/bin/env python3
"""
update_ips.py

- 从指定 URL 下载 ip.txt（默认放在 env var DOWNLOAD_URL 中）。
- 若下载内容与项目根目录的 custom_ips.txt 不同，则覆盖该文件并返回 1。
- 内容相同则返回 0（GitHub Actions 不会提交）。
"""

import hashlib
import os
import sys
from pathlib import Path

import requests

# -------------------------------------------------------------
# 配置
# -------------------------------------------------------------
DOWNLOAD_URL = os.getenv(
    "DOWNLOAD_URL",
    "https://example.com/path/to/ip.txt"  # <-- 替换成你的真实地址
)

# 项目根目录（相对于本文件所在的 scripts/ 目录向上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGET_FILE = PROJECT_ROOT / "custom_ips.txt"

# -------------------------------------------------------------
# 工具函数
# -------------------------------------------------------------
def sha256_of_bytes(data: bytes) -> str:
    """返回 data 的 SHA‑256 十六进制字符串。"""
    return hashlib.sha256(data).hexdigest()


def download_ip_file(url: str) -> bytes:
    """下载远程文件并返回原始 bytes。抛出异常代表下载失败。"""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    if not resp.content:
        raise ValueError("下载得到的文件为空")
    return resp.content


def load_existing() -> bytes:
    """读取已有的 custom_ips.txt（如果不存在则返回空 bytes）。"""
    if TARGET_FILE.is_file():
        return TARGET_FILE.read_bytes()
    return b""


def write_target(data: bytes) -> None:
    """原子写入 custom_ips.txt（先写临时文件，再 rename）。"""
    tmp_path = TARGET_FILE.with_suffix(".tmp")
    tmp_path.write_bytes(data)
    tmp_path.replace(TARGET_FILE)   # 原子替换
    print(f"✅ 已写入 {TARGET_FILE}")


def generate_summary(new_hash: str, new_len: int) -> None:
    """生成简短的 markdown 摘要，便于在 PR/commit 中查看。"""
    summary_path = PROJECT_ROOT / "custom_ips_summary.md"
    summary = f"""# IP 列表更新摘要

- **更新时间**：{Path.cwd().as_posix()}  (UTC {os.popen('date -u "+%Y-%m-%d %H:%M:%S"').read().strip()})
- **文件**：`custom_ips.txt`
- **SHA‑256**：`{new_hash}`
- **行数**：{new_len}
"""

    summary_path.write_text(summary, encoding="utf-8")
    print(f"📝 生成摘要 {summary_path}")


# -------------------------------------------------------------
# 主流程
# -------------------------------------------------------------
def main() -> int:
    try:
        # 1️⃣ 下载新内容
        print(f"🔽 正在下载 {DOWNLOAD_URL} ...")
        new_content = download_ip_file(DOWNLOAD_URL)

        # 2️⃣ 计算哈希
        new_hash = sha256_of_bytes(new_content)
        old_content = load_existing()
        old_hash = sha256_of_bytes(old_content) if old_content else ""

        # 3️⃣ 对比
        if old_content and new_hash == old_hash:
            print("ℹ️ 内容未变化，保持现有文件。")
            return 0          # 0 → 没有变动，GitHub Actions 不会提交

        # 4️⃣ 有变化 → 写入文件
        write_target(new_content)

        # 5️⃣ 生成摘要（可选）
        generate_summary(new_hash, new_content.decode(errors="ignore").count("\n") + 1)

        # 6️⃣ 返回 1 表示“有变动”，让 workflow 继续提交
        return 1

    except Exception as exc:
        print(f"❌ 运行出错: {exc}", file=sys.stderr)
        return 2   # 非 0 码会让 workflow 直接失败，便于排查


if __name__ == "__main__":
    sys.exit(main())
