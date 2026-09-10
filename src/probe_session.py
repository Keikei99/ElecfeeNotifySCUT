#!/usr/bin/env python3
"""探测当前 JSESSIONID 的有效期：每隔一段时间打一次受保护接口，记录何时失效。

用法:
  python3 src/probe_session.py                 # 每 60 秒探测一次，直到失效后停止
  python3 src/probe_session.py --interval 30   # 自定义间隔（秒）
  python3 src/probe_session.py --keep-going    # 失效后继续探测（不退出）

⚠️ 重要：调用接口本身算一次“活动”，会重置服务器端的空闲计时器。
  - 若服务器是“空闲超时”（如 30 分钟无活动才失效）：频繁探测会让会话一直不失效，
    这反而说明「定期轻量探测即可保活」。想测纯空闲寿命，应把 --interval 设得比空闲窗口大，
    或干脆别探测、隔一段时间手动跑一次。
  - 若服务器有“绝对寿命上限”：本脚本能精确测到那个时间点。

每次探测都会打印，并追加写入 log/probe.log。
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

# 统一东八区（北京时间），避免服务器 UTC 时区导致时间少 8 小时
os.environ["TZ"] = os.environ.get("ELECFEE_TZ", "Asia/Shanghai")
try:
    time.tzset()
except AttributeError:
    pass

import yaml

from scut_client import ScutClient, SessionExpired

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.yaml")
LOG_PATH = os.path.join(PROJECT_ROOT, "log", "probe.log")


def out(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fmt_dur(seconds):
    return str(timedelta(seconds=int(seconds)))


def main():
    ap = argparse.ArgumentParser(description="探测 JSESSIONID 有效期")
    ap.add_argument("--interval", type=float, default=60, help="探测间隔（秒），默认 60")
    ap.add_argument("--keep-going", action="store_true", help="失效后继续探测，不退出")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(CONFIG_PATH, encoding="utf-8"))
    jsessionid = cfg["session"]["jsessionid"]
    client = ScutClient(jsessionid=jsessionid)

    start = time.time()
    last_ok = None
    n = 0
    out(f"开始探测（间隔 {args.interval:g}s，JSESSIONID 前8位 {jsessionid[:8]}…）")
    out("提示：每次探测都是一次活动，可能重置空闲计时器（见脚本说明）。")

    while True:
        n += 1
        elapsed = time.time() - start
        try:
            u = client.userinfo(refresh=True)
            last_ok = time.time()
            out(f"#{n:>4}  运行 {fmt_dur(elapsed)}  ✅ 有效  "
                f"（{u.get('realName')} / {u.get('roomName')}）")
        except SessionExpired:
            since_ok = (time.time() - last_ok) if last_ok else None
            out(f"#{n:>4}  运行 {fmt_dur(elapsed)}  ❌ 已失效")
            if last_ok:
                out(f"==> 会话失效。最后一次成功在 {datetime.fromtimestamp(last_ok):%H:%M:%S}，"
                    f"距上次成功约 {fmt_dur(since_ok)}；自探测开始约 {fmt_dur(elapsed)}。")
            else:
                out("==> 首次探测即失效：请先用 --login 刷新会话再测。")
            if not args.keep_going:
                return 0
        except Exception as e:
            out(f"#{n:>4}  运行 {fmt_dur(elapsed)}  ⚠️ 探测异常：{e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        out("已手动停止探测。")
