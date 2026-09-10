"""通知渠道：邮件(SMTP) 与 微信推送(Server酱/PushPlus) + 邮件 HTML 模板渲染。

邮件正文来自 email_page/ 目录下的 HTML 模板，可自由编辑。
模板用 {{变量}} 占位，并可用 <!-- subject: 标题{{变量}} --> 指定邮件标题。
"""
import os
import re
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
TPL_DIR = os.path.join(PROJECT_ROOT, "email_page")


def render(template_name, context, default_subject=""):
    """读取 email_page/<template_name>，替换 {{变量}}，返回 (subject, html_body)。

    模板首个 <!-- subject: ... --> 注释作为邮件标题（会被移出正文）。
    """
    path = os.path.join(TPL_DIR, template_name)
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    subject = default_subject
    m = re.search(r"<!--\s*subject:(.*?)-->", text, flags=re.S | re.I)
    if m:
        subject = m.group(1).strip()
        text = (text[:m.start()] + text[m.end():])

    for k, v in context.items():
        token = "{{" + k + "}}"
        text = text.replace(token, str(v))
        subject = subject.replace(token, str(v))
    return subject, text


def send_email(smtp_cfg, to_addrs, subject, body, html=False):
    """通过 SMTP 发邮件。to_addrs 可为字符串或列表；html=True 时发送 HTML 正文。"""
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    to_addrs = [a for a in to_addrs if a]
    if not to_addrs:
        raise ValueError("没有可用的收件邮箱")

    msg = MIMEText(body, "html" if html else "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    from_addr = smtp_cfg.get("from_addr") or smtp_cfg["username"]
    msg["From"] = formataddr((str(Header("电费助手", "utf-8")), from_addr))
    msg["To"] = ", ".join(to_addrs)

    host = smtp_cfg["host"]
    port = int(smtp_cfg.get("port", 465))
    if smtp_cfg.get("use_ssl", True):
        server = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        server.starttls()
    try:
        server.login(smtp_cfg["username"], smtp_cfg["password"])
        server.sendmail(from_addr, to_addrs, msg.as_string())
    finally:
        server.quit()


def send_wechat(wechat_cfg, title, body):
    """微信推送。支持 provider: serverchan / pushplus。"""
    provider = (wechat_cfg.get("provider") or "").lower()
    token = wechat_cfg.get("token")
    if not token:
        raise ValueError("微信推送未配置 token")
    if provider == "serverchan":
        url = f"https://sctapi.ftqq.com/{token}.send"
        r = requests.post(url, data={"title": title, "desp": body}, timeout=20)
    elif provider == "pushplus":
        r = requests.post("https://www.pushplus.plus/send",
                          json={"token": token, "title": title, "content": body},
                          timeout=20)
    else:
        raise ValueError(f"未知的微信推送 provider: {provider}")
    r.raise_for_status()
    return r.text
