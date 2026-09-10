#!/usr/bin/env python3
"""华工宿舍电费通知系统 —— 入口。

用法:
  python3 src/main.py            # 执行一轮检查（配合系统 cron 每天调用一次）
  python3 src/main.py --once     # 同上，跑一轮
  python3 src/main.py --check-room C1 101    # 临时查某个房间的余额（不发通知）
  python3 src/main.py --dry-run  # 查询并判断，但不真正发通知（用于测试）
  python3 src/main.py --login    # 交互式登录（含二次认证），刷新会话
  python3 src/main.py --keepalive                     # 会话保活（供 cron 每~20分钟调用）
  python3 src/main.py --test-email x@example.com      # 发测试邮件（省略则发给管理员邮箱）
  python3 src/main.py --test-sub C1-101 x@example.com # 测试订阅：查该房间并把测试邮件发到该邮箱
  python3 src/main.py --test-csv csv/test.example.csv # 批量测试：CSV逐行发订阅测试邮件，随机间隔
  python3 src/main.py --add-sub C1-101 a@example.com 10 # 新增订阅：房间 邮箱 阈值(元)
  python3 src/main.py --import-csv csv/subscriptions.example.csv # CSV 批量导入
  python3 src/main.py --remove-sub C1-101 a@example.com # 删除某人对该房间的订阅
  python3 src/main.py --remove-sub C1-101            # 删除该房间的全部订阅
  python3 src/main.py --list-subs                     # 列出所有订阅

时区：所有时间统一按北京时间(Asia/Shanghai)显示，服务器在 UTC 也不会差 8 小时。
      如需改时区，设环境变量 ELECFEE_TZ。

设计要点：
  - 所有房间查询串行执行（共用一个会话的绑定态，不能并发）。
  - 每个房间有冷却期，低于阈值后 cooldown_hours 内不重复通知。
  - 会话失效(SessionExpired)会明确提示更新 JSESSIONID / 启用自动登录。
"""
import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime

# 统一用东八区(北京时间)显示/记录所有时间，避免服务器在 UTC 时区时日志/邮件时间少 8 小时。
# 通过设置进程 TZ 环境变量 + tzset()，让 datetime.now() / fromtimestamp() 全部按北京时间。
os.environ["TZ"] = os.environ.get("ELECFEE_TZ", "Asia/Shanghai")
try:
    time.tzset()
except AttributeError:  # Windows 无 tzset，忽略（Linux/macOS 正常）
    pass

import yaml

from scut_client import ScutClient, SessionExpired
from resolver import RoomResolver
import notifier

# 日志：统一走标准 logging。日志只写到 stdout，由系统 cron 的重定向
# （如 `>> log/run.log 2>&1`）落盘，日志轮转交给操作系统（logrotate）处理，
# Python 不自己管文件与切割。时间戳走本地时区（前面已 tzset 成北京时间）。
logger = logging.getLogger("elecfee")


def _setup_logging():
    if logger.handlers:  # 避免重复导入/多次调用时挂多个 handler
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_setup_logging()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
JSON_DIR = os.path.join(PROJECT_ROOT, "json")

APP_ENV = os.getenv("ELECFEE_ENV", "dev").lower()   # dev / prod


if APP_ENV == "dev":
    # 开发环境
    CONFIG_PATH = os.path.join(CONFIG_DIR, "config_dev.yaml")
    SUBS_PATH = os.path.join(CONFIG_DIR, "subscriptions_dev.yaml")
elif APP_ENV == "prod":
    # 生产环境
    CONFIG_PATH = os.path.join(CONFIG_DIR, "config_prods.yaml")
    SUBS_PATH = os.path.join(CONFIG_DIR, "subscriptions_prods.yaml")
else:
    logger.warning(f"ELECFEE_ENV={APP_ENV} 未知，默认 dev")
    CONFIG_PATH = os.path.join(CONFIG_DIR, "config_dev.yaml")
    SUBS_PATH = os.path.join(CONFIG_DIR, "subscriptions_dev.yaml")

STATE_PATH = os.path.join(JSON_DIR, "state.json")
CACHE_PATH = os.path.join(JSON_DIR, "rooms_cache.json")


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---- 订阅模型：{ "房间号": [ {email, threshold}, ... ] } ----
SUBS_HEADER = (
    "# 订阅表：每个房间 -> 订阅者列表，每个订阅者有自己的邮箱和最低通知阈值(元)。\n"
    "# 结构：{ 房间号: [ {email: 邮箱, threshold: 阈值}, ... ] }\n"
    "# 用命令管理（推荐）：\n"
    "#   python3 src/main.py --add-sub C1-101 a@example.com 10\n"
    "#   python3 src/main.py --remove-sub C1-101 a@example.com\n"
    "#   python3 src/main.py --list-subs\n"
    "# 也可手动编辑本文件（用命令写入时本注释头会保留，其余排版可能被重排）。\n\n"
)


def load_subs():
    """返回 dict: {room: [ {email, threshold(float)}, ... ] }。"""
    if not os.path.exists(SUBS_PATH):
        return {}
    data = load_yaml(SUBS_PATH) or {}
    subs = {}
    for room, lst in data.items():
        items = []
        for it in (lst or []):
            if isinstance(it, dict):
                email = it.get("email") or it.get("邮箱")
                thr = it.get("threshold", it.get("阈值"))
            elif isinstance(it, (list, tuple)):  # 兼容 [邮箱, 阈值]
                email = it[0]
                thr = it[1] if len(it) > 1 else None
            else:
                email, thr = it, None
            if not email:
                continue
            items.append({"email": str(email),
                          "threshold": None if thr is None else float(thr)})
        if items:
            subs[str(room)] = items
    return subs


def save_subs(subs):
    plain = {room: [{"email": s["email"], "threshold": s["threshold"]} for s in lst]
             for room, lst in subs.items()}
    with open(SUBS_PATH, "w", encoding="utf-8") as f:
        f.write(SUBS_HEADER)
        yaml.safe_dump(plain, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(JSON_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)





def build_client(config):
    """优先自动登录（若配置了学号密码），否则用 JSESSIONID。"""
    creds = config.get("credentials") or {}
    if creds.get("enabled") and creds.get("student_id") and creds.get("password"):
        try:
            from scut_login import login  # 延迟导入，未启用时不依赖
            jsessionid = login(creds["student_id"], creds["password"])
            logger.info("自动登录成功，已获取新会话")
            return ScutClient(jsessionid=jsessionid)
        except Exception as e:
            logger.warning(f"自动登录失败({e})，回退到 config.session.jsessionid")
    return ScutClient(jsessionid=config["session"]["jsessionid"])


def _query_interval(config):
    """房间之间的随机间隔范围(秒)。配置 check.query_interval：
    可写单个数字(固定)或 [min, max]（随机）；缺省 [5, 6]。"""
    qi = config.get("check", {}).get("query_interval", [5, 6])
    if isinstance(qi, (int, float)):
        return float(qi), float(qi)
    lo, hi = float(qi[0]), float(qi[1])
    return (lo, hi) if lo <= hi else (hi, lo)


def fmt_balance(bal):
    return (f"剩余充值 {bal['leftEle']} 度 / {bal['leftMoney']} 元；"
            f"补助 {bal['leftFreeEle']} 度 / {bal['leftFreeMoney']} 元；"
            f"单价 {bal['elePrice']} 元/度")


def _mail_context(bal, email, room_name, threshold=None):
    """把电表余额组织成邮件模板可用的变量字典。"""
    return {
        "room_name": room_name,
        "left_money": bal["leftMoney"],
        "left_ele": bal["leftEle"],
        "left_free_money": bal["leftFreeMoney"],
        "left_free_ele": bal["leftFreeEle"],
        "ele_price": bal["elePrice"],
        "mon_time": datetime.fromtimestamp(bal["monTime"] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        "threshold": "" if threshold is None else threshold,
        "email": email,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def run_once(config, subs, dry_run=False):
    resolver = RoomResolver(CACHE_PATH)
    client = build_client(config)
    state = load_state()

    try:
        u = client.userinfo()
        logger.info(f"会话有效，账号：{u.get('realName')} ({u.get('studentNum')})")
    except SessionExpired as e:
        logger.error(f"❌ {e}")
        logger.error("请运行 python3 src/main.py --login 刷新会话，或更新 config.yaml 的 session.jsessionid。")
        notify_session_expired(config)
        return 2

    now = time.time()
    cooldown = float(config.get("check", {}).get("cooldown_hours", 24)) * 3600
    default_thr = float(config.get("defaults", {}).get("threshold_money", 10.0))
    qmin, qmax = _query_interval(config)
    triggered = 0
    first_query = True

    for room, subscribers in subs.items():
        try:
            loc = resolver.resolve_full(room)
        except LookupError as e:
            logger.warning(f"[{room}] 解析房间失败：{e}")
            continue

        # 房间之间随机间隔（串行查询，避免背靠背打服务器/被风控）
        if not first_query:
            time.sleep(random.uniform(qmin, qmax))
        first_query = False

        try:
            bal = client.query_room(loc["loudongId"], loc["loucengId"], loc["roomId"])
        except SessionExpired as e:
            logger.error(f"❌ 查询中途会话失效：{e}")
            notify_session_expired(config)
            return 2

        if bal is None:
            logger.warning(f"[{room}] {loc['roomName']} 无电表或查询失败，跳过")
            continue

        left_money = float(bal["leftMoney"])
        logger.info(f"[{loc['roomName']}] {fmt_balance(bal)}  订阅者 {len(subscribers)} 人")

        key = str(loc["roomId"])
        st = state.get(key, {})
        st["last_balance"] = left_money
        st["last_check"] = now
        st["room_name"] = loc["roomName"]
        notified = st.get("notified", {})

        # 每个订阅者独立判断阈值+冷却，并各自单独收到一封（含自己的阈值、互不可见）
        room_triggered = False
        for sub in subscribers:
            email = sub["email"]
            thr = sub["threshold"] if sub["threshold"] is not None else default_thr
            if left_money >= thr:
                continue
            last = notified.get(email, 0)
            if now - last < cooldown:
                mins = int((cooldown - (now - last)) / 60)
                logger.info(f"    {email} 低于其阈值{thr}元，但在冷却期内（约{mins}分钟），跳过")
                continue
            if dry_run:
                logger.info(f"    [dry-run] 将通知：{email}（阈值 {thr} 元）")
            else:
                try:
                    ctx = _mail_context(bal, email, loc["roomName"], thr)
                    subject, html = notifier.render("alert.html", ctx)
                    notifier.send_email(config["smtp"], email, subject, html, html=True)
                    notified[email] = now
                    logger.info(f"    已通知：{email}（阈值 {thr} 元）")
                except Exception as e:
                    logger.warning(f"    通知 {email} 失败：{e}")
            room_triggered = True

        # 微信推送（可选）：同房间触发时按房间推一条（token 是全局的，通常给房主）
        if room_triggered and not dry_run and config.get("wechat", {}).get("token"):
            try:
                notifier.send_wechat(
                    config["wechat"], f"⚡电费预警：{loc['roomName']} 仅剩 {left_money} 元",
                    fmt_balance(bal))
            except Exception as e:
                logger.warning(f"    微信推送失败：{e}")

        if room_triggered:
            triggered += 1
        st["notified"] = notified
        state[key] = st

    restore_home_room(client, config, resolver)

    save_state(state)
    logger.info(f"本轮完成，触发通知 {triggered} 次。")
    return 0


def restore_home_room(client, config, resolver=None):
    """把会话绑回 check.home_room（避免停在最后查询的房间，影响房主微信里看到的房间）。

    任何会调用 query_room 的命令查完都应调用它；出错只记录、不影响主流程。
    """
    if not config.get("check", {}).get("restore_home_room"):
        return
    hr = config.get("check", {}).get("home_room")
    if not hr:
        return
    resolver = resolver or RoomResolver(CACHE_PATH)
    try:
        loc = resolver.resolve(hr["building"], hr["room"])
        client.set_room(loc["loudongId"], loc["loucengId"], loc["roomId"])
        logger.info(f"已将会话绑回主房间 {loc['roomName']}")
    except Exception as e:
        logger.warning(f"绑回主房间失败（不影响功能）：{e}")


def set_config_jsessionid(new_value):
    """把新的 JSESSIONID 写回 config.yaml（文本替换，保留注释与格式）。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    import re
    new_text, n = re.subn(
        r'(?m)^(\s*jsessionid:\s*).*$',
        lambda m: f'{m.group(1)}"{new_value}"', text, count=1)
    if n == 0:
        raise RuntimeError("未在 config.yaml 找到 session.jsessionid 行")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(new_text)


def do_login(config):
    """交互式登录：用配置里的学号密码 + 手动输入二次认证码，换取新会话并写回 config。"""
    from scut_login import login, TwoFactorRequired, CaptchaRequired, LoginFailed
    creds = config.get("credentials") or {}
    sid, pw = creds.get("student_id"), creds.get("password")
    if not sid or not pw:
        logger.error("请先在 config/config.yaml 的 credentials 填写 student_id 和 password。")
        return 2

    def ask_code():
        logger.info("账号已开启二次认证。请查看微信企业号「华南理工大学」或短信收到的验证码（90秒有效）。")
        return input("请输入二次认证验证码: ").strip()

    try:
        logger.info("正在登录（提交账号密码）……")
        jsessionid = login(sid, pw, code_provider=ask_code)
    except CaptchaRequired as e:
        logger.error(f"❌ {e}")
        return 2
    except LoginFailed as e:
        logger.error(f"❌ 登录失败：{e}")
        return 2
    except TwoFactorRequired as e:
        logger.error(f"❌ {e}")
        return 2

    set_config_jsessionid(jsessionid)
    logger.info(f"✅ 登录成功，新会话已写入 config.yaml（JSESSIONID 前8位 {jsessionid[:8]}…）")
    # 顺带校验一下
    client = ScutClient(jsessionid=jsessionid)
    u = client.userinfo()
    logger.info(f"会话可用，账号：{u.get('realName')} ({u.get('studentNum')})")
    return 0


def notify_session_expired(config):
    """会话失效时给“管理员邮箱”发一封提醒邮件（12 小时内不重复发）。

    收件人取 check.session_alert_email，缺省回退到 smtp.from_addr / smtp.username。
    这封提醒是发给能重新登录的人，而不是订阅电费的用户。
    """
    state = load_state()
    now = time.time()
    if now - state.get("_session_alert", 0) < 12 * 3600:
        return
    smtp = config.get("smtp", {})
    admin = (config.get("check", {}).get("session_alert_email")
             or smtp.get("from_addr") or smtp.get("username"))
    if not admin:
        return
    try:
        subject, html = notifier.render(
            "session_expired.html",
            {"now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        notifier.send_email(smtp, admin, subject, html, html=True)
        state["_session_alert"] = now
        save_state(state)
        logger.info(f"已发送会话失效提醒邮件给管理员：{admin}")
    except Exception as e:
        logger.warning(f"发送会话失效提醒失败：{e}")


def do_test(config, email=None):
    """发送一封测试邮件（email_page/test.html）。省略邮箱则发给管理员邮箱。"""
    smtp = config.get("smtp", {})
    to = email or (config.get("check", {}).get("session_alert_email")
                   or smtp.get("from_addr") or smtp.get("username"))
    if not to:
        logger.error("没有可用收件人：请提供邮箱，例如 --test-email x@example.com")
        return 2
    try:
        subject, html = notifier.render("test.html", {
            "email": to, "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        notifier.send_email(smtp, to, subject, html, html=True)
        logger.info(f"✅ 测试邮件已发送给：{to}")
        return 0
    except Exception as e:
        logger.error(f"❌ 测试邮件发送失败：{e}")
        return 1


def _send_sub_test(client, resolver, config, room, email):
    """查一次该房间余额并把订阅测试邮件发到 email。会话失效会向上抛 SessionExpired。"""
    try:
        loc = resolver.resolve_full(room)
    except LookupError as e:
        logger.error(f"❌ 房间号无法识别：{room}（{e}）")
        return 1
    bal = client.query_room(loc["loudongId"], loc["loucengId"], loc["roomId"])
    if bal is None:
        logger.error(f"❌ {loc['roomName']} 无电表或查询失败")
        return 1
    logger.info(f"[{loc['roomName']}] {fmt_balance(bal)} -> {email}")
    try:
        ctx = _mail_context(bal, email, loc["roomName"])
        subject, html = notifier.render("test_sub.html", ctx)
        notifier.send_email(config["smtp"], email, subject, html, html=True)
        logger.info(f"✅ 订阅测试邮件已发送给：{email}")
        return 0
    except Exception as e:
        logger.error(f"❌ 订阅测试邮件发送失败 {email}：{e}")
        return 1


def do_test_sub(config, room, email):
    """测试订阅：解析房间→查询余额→把订阅测试邮件发到该邮箱（无论是否低于阈值都发）。"""
    client = build_client(config)
    resolver = RoomResolver(CACHE_PATH)
    try:
        client.userinfo()
        rc = _send_sub_test(client, resolver, config, room, email)
        restore_home_room(client, config, resolver)
        return rc
    except SessionExpired as e:
        logger.error(f"❌ 会话失效，无法查询：{e}（请先 python3 src/main.py --login）")
        return 2


def _test_interval(config):
    """批量测试的随机间隔范围(秒)。配置 check.test_interval：数字或 [min,max]；缺省 [5,15]。"""
    ti = config.get("check", {}).get("test_interval", [5, 15])
    if isinstance(ti, (int, float)):
        return float(ti), float(ti)
    lo, hi = float(ti[0]), float(ti[1])
    return (lo, hi) if lo <= hi else (hi, lo)


def do_test_csv(config, csv_path):
    """批量测试：CSV(房间号,邮箱[,阈值]) 逐行发订阅测试邮件，每两次之间随机间隔。"""
    if not os.path.exists(csv_path):
        logger.error(f"❌ 找不到文件：{csv_path}")
        return 2
    entries = []
    for row in _read_csv_rows(csv_path):
        if len(row) < 2:
            continue
        room, email = row[0].strip(), row[1].strip()
        if not room or "@" not in email:  # 跳过空行/表头
            continue
        entries.append((room, email))
    if not entries:
        logger.warning("CSV 中没有有效的 (房间号,邮箱) 行。")
        return 2

    client = build_client(config)
    resolver = RoomResolver(CACHE_PATH)
    try:
        client.userinfo()
    except SessionExpired as e:
        logger.error(f"❌ 会话失效，无法批量测试：{e}（请先 python3 src/main.py --login）")
        return 2

    lo, hi = _test_interval(config)
    logger.info(f"开始批量测试，共 {len(entries)} 条，间隔随机 {lo:g}~{hi:g} 秒。")
    ok = fail = 0
    for i, (room, email) in enumerate(entries, 1):
        if i > 1:
            d = random.uniform(lo, hi)
            logger.info(f"  ⏳ 等待 {d:.1f}s 后进行第 {i}/{len(entries)} 条……")
            time.sleep(d)
        try:
            r = _send_sub_test(client, resolver, config, room, email)
        except SessionExpired:
            logger.error("❌ 会话中途失效，已停止批量测试（请 --login 后重试）。")
            return 2
        ok += 1 if r == 0 else 0
        fail += 1 if r != 0 else 0
    restore_home_room(client, config, resolver)
    logger.info(f"批量测试完成：成功 {ok}，失败 {fail}。")
    return 0 if fail == 0 else 1


def _canon_room(room):
    """归一化房间号用于对照解析（保留原样作为存储键）。"""
    return str(room).strip().upper()


def add_sub(config, room, email, threshold):
    """新增/更新一个订阅：房间号 + 邮箱 + 最低阈值。"""
    room = _canon_room(room)
    email = email.strip()
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        logger.error(f"阈值必须是数字：{threshold}")
        return 2
    # 校验房间号确实存在（能解析）
    try:
        loc = RoomResolver(CACHE_PATH).resolve_full(room)
        room = loc["roomName"]  # 用标准房间名作为键
    except LookupError as e:
        logger.error(f"❌ 房间号无法识别：{e}")
        return 2

    subs = load_subs()
    lst = subs.get(room, [])
    for s in lst:
        if s["email"].lower() == email.lower():
            s["threshold"] = threshold
            save_subs(subs)
            logger.info(f"✅ 已更新订阅：{room}  {email}  阈值 {threshold} 元")
            return 0
    lst.append({"email": email, "threshold": threshold})
    subs[room] = lst
    save_subs(subs)
    logger.info(f"✅ 已新增订阅：{room}  {email}  阈值 {threshold} 元")
    return 0


def remove_sub(config, room, email=None):
    """删除订阅。email 省略时删除该房间的**所有**订阅；否则只删该邮箱那条。"""
    room = _canon_room(room)
    subs = load_subs()
    # 房间键可能是标准名，尝试解析对齐
    key = room
    if key not in subs:
        try:
            key = RoomResolver(CACHE_PATH).resolve_full(room)["roomName"]
        except LookupError:
            pass
    lst = subs.get(key)
    if not lst:
        logger.warning(f"未找到房间 {room} 的订阅。")
        return 1

    if email is None:
        n = len(lst)
        del subs[key]
        save_subs(subs)
        logger.info(f"✅ 已删除房间 {key} 的全部订阅（{n} 条）。")
        return 0

    email = email.strip()
    new = [s for s in lst if s["email"].lower() != email.lower()]
    if len(new) == len(lst):
        logger.warning(f"房间 {key} 下未找到邮箱 {email} 的订阅。")
        return 1
    if new:
        subs[key] = new
    else:
        del subs[key]
    save_subs(subs)
    logger.info(f"✅ 已删除订阅：{key}  {email}")
    return 0


def _read_csv_rows(csv_path):
    """读 CSV 文本行，兼容 Excel 的 UTF-8(BOM) 与 GBK 编码。"""
    import csv
    with open(csv_path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise RuntimeError("无法识别 CSV 编码（请另存为 UTF-8 或 GBK）")
    return list(csv.reader(text.splitlines()))


def import_csv(config, csv_path):
    """从 CSV 批量导入订阅。3 列：房间号, 邮箱, 阈值(元)。可带表头（自动跳过）。"""
    if not os.path.exists(csv_path):
        logger.error(f"❌ 找不到文件：{csv_path}")
        return 2
    rows = _read_csv_rows(csv_path)
    resolver = RoomResolver(CACHE_PATH)
    subs = load_subs()
    added = updated = skipped = 0
    errors = []

    for i, row in enumerate(rows, 1):
        if len(row) < 3:
            if any(c.strip() for c in row):
                skipped += 1
            continue
        room_raw, email, thr_raw = row[0].strip(), row[1].strip(), row[2].strip()
        if not room_raw or not email or "@" not in email:
            skipped += 1  # 空行或表头（如“房间号,邮箱,阈值”）
            continue
        try:
            thr = float(thr_raw)
        except ValueError:
            skipped += 1  # 阈值非数字，多半是表头
            continue
        try:
            room = resolver.resolve_full(room_raw)["roomName"]
        except LookupError:
            errors.append(f"第{i}行 房间无法识别：{room_raw}")
            continue

        lst = subs.get(room, [])
        for s in lst:
            if s["email"].lower() == email.lower():
                s["threshold"] = thr
                updated += 1
                break
        else:
            lst.append({"email": email, "threshold": thr})
            subs[room] = lst
            added += 1

    save_subs(subs)
    logger.info(f"✅ 导入完成：新增 {added}，更新 {updated}，跳过 {skipped}，失败 {len(errors)}。")
    for e in errors:
        logger.warning(f"   {e}")
    return 0 if not errors else 1


def list_subs(config):
    subs = load_subs()
    if not subs:
        logger.info("当前没有任何订阅。")
        return 0
    total = sum(len(v) for v in subs.values())
    logger.info(f"当前订阅（{len(subs)} 个房间 / {total} 条）：")
    default_thr = config.get("defaults", {}).get("threshold_money", 10.0)
    for room in sorted(subs):
        for s in subs[room]:
            thr = s["threshold"] if s["threshold"] is not None else f"{default_thr}(默认)"
            logger.info(f"  {room}  ->  {s['email']}  (阈值 {thr} 元)")
    return 0


def do_keepalive(config):
    """会话保活：轻量打一次 userinfo，重置服务器端空闲计时器。

    供 cron 每隔 ~20 分钟调用一次，让每日检查时总有可用会话。
    读 userinfo 保活，并把会话绑回 home_room（这样即使之前某次测试把绑定
    留在别的房间，保活也会把它拉回主房间）。若已失效则发提醒邮件（12h 冷却）。
    """
    client = ScutClient(jsessionid=config["session"]["jsessionid"])
    try:
        u = client.userinfo(refresh=True)
        logger.info(f"保活成功：会话有效（{u.get('realName')} / {u.get('roomName')}）")
        restore_home_room(client, config)
        return 0
    except SessionExpired:
        logger.error("❌ 保活失败：会话已失效，请运行 python3 src/main.py --login 刷新。")
        notify_session_expired(config)
        return 2
    except Exception as e:
        logger.warning(f"⚠️ 保活探测异常：{e}")
        return 1


def check_room(config, building, room):
    """临时查询单个房间余额，不发通知。"""
    resolver = RoomResolver(CACHE_PATH)
    client = build_client(config)
    client.userinfo()
    loc = resolver.resolve(building, room)
    bal = client.query_room(loc["loudongId"], loc["loucengId"], loc["roomId"])
    if bal is None:
        logger.warning(f"{loc['roomName']} 无电表或查询失败")
        restore_home_room(client, config, resolver)
        return 1
    logger.info(f"{loc['roomName']}  {fmt_balance(bal)}")
    restore_home_room(client, config, resolver)
    return 0


def main():
    ap = argparse.ArgumentParser(description="华工宿舍电费通知系统")
    ap.add_argument("--once", action="store_true", help="执行一轮检查（默认行为）")
    ap.add_argument("--dry-run", action="store_true", help="查询并判断但不真正发通知")
    ap.add_argument("--check-room", nargs=2, metavar=("BUILDING", "ROOM"),
                    help="临时查询某房间余额，例如 --check-room C1 101")
    ap.add_argument("--login", action="store_true",
                    help="交互式登录（含二次认证），刷新 config 里的 JSESSIONID")
    ap.add_argument("--keepalive", action="store_true",
                    help="会话保活：轻量探测一次以重置空闲计时（供 cron 每~20分钟调用）")
    ap.add_argument("--test-email", "--test", dest="test_email", nargs="?", const="",
                    metavar="EMAIL", help="发送测试邮件到指定邮箱；省略邮箱则发给管理员邮箱")
    ap.add_argument("--test-sub", nargs=2, metavar=("ROOM", "EMAIL"),
                    help="测试订阅：查该房间余额并把测试邮件发到该邮箱，例如 --test-sub C1-101 a@example.com")
    ap.add_argument("--test-csv", metavar="FILE",
                    help="批量测试：CSV(房间号,邮箱[,阈值]) 逐行发订阅测试邮件，间隔随机时间")
    ap.add_argument("--add-sub", nargs=3, metavar=("ROOM", "EMAIL", "THRESHOLD"),
                    help="新增订阅：房间号 邮箱 最低阈值(元)，例如 --add-sub C1-101 a@example.com 10")
    ap.add_argument("--remove-sub", nargs="+", metavar="ROOM [EMAIL]",
                    help="删除订阅：给 房间号 邮箱 只删一条；只给 房间号 则删该房间全部订阅")
    ap.add_argument("--import-csv", metavar="FILE",
                    help="从 CSV 批量导入订阅（3列：房间号,邮箱,阈值），例如 --import-csv csv/subscriptions.example.csv")
    ap.add_argument("--list-subs", action="store_true", help="列出所有订阅")
    args = ap.parse_args()

    config = load_yaml(CONFIG_PATH)

    if args.login:
        return do_login(config)
    if args.keepalive:
        return do_keepalive(config)
    if args.list_subs:
        return list_subs(config)
    if args.add_sub:
        return add_sub(config, args.add_sub[0], args.add_sub[1], args.add_sub[2])
    if args.import_csv:
        return import_csv(config, args.import_csv)
    if args.remove_sub:
        rs = args.remove_sub
        if len(rs) > 2:
            logger.error("--remove-sub 最多 2 个参数：房间号 [邮箱]")
            return 2
        return remove_sub(config, rs[0], rs[1] if len(rs) == 2 else None)
    if args.test_csv:
        return do_test_csv(config, args.test_csv)
    if args.test_sub:
        return do_test_sub(config, args.test_sub[0], args.test_sub[1])
    if args.test_email is not None:
        return do_test(config, args.test_email or None)
    if args.check_room:
        return check_room(config, args.check_room[0], args.check_room[1])

    subs = load_subs()
    return run_once(config, subs, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
