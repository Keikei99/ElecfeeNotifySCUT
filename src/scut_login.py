"""CAS 自动登录：用学号+密码换取 sdms-weixin-pay 的有效 JSESSIONID。

复刻前端 login2.js 的提交逻辑（智能验证码关闭时的分支）：
    ul = 用户名长度, pl = 密码长度
    rsa = strEnc(用户名 + 密码 + lt, '1', '2', '3')   # 由 des.js 计算
    POST /cas/login?service=...  字段: rsa, ul, pl, lt, execution, _eventId=submit

⚠️ 未在真实账号上验证过（需要密码）。若学校开启了智能验证码(is_open_captcha=1)，
   本模块会检测到并抛出 CaptchaRequired，此时需降级为手动更新 JSESSIONID。
"""
import os
import re
import ssl
import subprocess

import requests
from requests.adapters import HTTPAdapter

HERE = os.path.dirname(os.path.abspath(__file__))


class _LegacyTLSAdapter(HTTPAdapter):
    """sso.scut.edu.cn 只支持老旧的 TLS_RSA_WITH_AES_256_CBC_SHA 套件，
    OpenSSL 3.0 默认列表不含它，需把安全级别降到 0（证书校验仍保留）。"""

    def _ctx(self):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        return ctx

    def init_poolmanager(self, *a, **k):
        k["ssl_context"] = self._ctx()
        return super().init_poolmanager(*a, **k)

    def proxy_manager_for(self, *a, **k):
        k["ssl_context"] = self._ctx()
        return super().proxy_manager_for(*a, **k)

CAS_BASE = "https://sso.scut.edu.cn/cas"
SERVICE_URL = "https://dfyc.utc.scut.edu.cn/sdms-weixin-pay/service/weixin/thirdLogin"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


class CaptchaRequired(Exception):
    pass


class LoginFailed(Exception):
    pass


class TwoFactorRequired(Exception):
    """密码正确，但账号开启了二次认证，需要提供动态验证码。"""


def _strenc(data, k1="1", k2="2", k3="3"):
    out = subprocess.run(
        ["node", os.path.join(HERE, "strenc.js"), data, k1, k2, k3],
        capture_output=True, text=True, timeout=20, check=True,
    )
    return out.stdout.strip()


def _sdms_jsessionid(session):
    for c in session.cookies:
        if c.name == "JSESSIONID" and "dfyc.utc.scut.edu.cn" in (c.domain or ""):
            return c.value
    return None


def login(student_id, password, code_provider=None):
    """用学号+密码登录，返回 sdms-weixin-pay 的 JSESSIONID。

    - 若账号无二次认证：直接返回会话。
    - 若开启二次认证：
        * 提供了 code_provider（一个返回验证码字符串的可调用对象）→ 交互式完成二次认证；
        * 未提供 → 抛 TwoFactorRequired。
    失败抛 LoginFailed / CaptchaRequired。
    """
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    s.mount("https://", _LegacyTLSAdapter())
    login_url = f"{CAS_BASE}/login"
    params = {"service": SERVICE_URL}

    # 1) 拉登录页，取 lt / execution，并检查验证码开关
    html = s.get(login_url, params=params, timeout=20).text
    m = re.search(r"is_open_captcha\s*=\s*'(\d)'", html)
    if m and m.group(1) == "1":
        raise CaptchaRequired("CAS 已开启智能验证码，无法自动登录，请改用手动 JSESSIONID。")
    lt = _hidden(html, "lt")
    execution = _hidden(html, "execution")
    if not lt or not execution:
        raise LoginFailed("未能从登录页解析 lt / execution")

    # 2) 提交账号密码（密码经 des.js strEnc 加密进 rsa）
    rsa = _strenc(f"{student_id}{password}{lt}")
    r2 = s.post(login_url, params=params, allow_redirects=True, timeout=20, data={
        "rsa": rsa, "ul": str(len(student_id)), "pl": str(len(password)),
        "lt": lt, "execution": execution, "_eventId": "submit",
        "choosenumber": "", "captcha": "",
    })

    jsessionid = _sdms_jsessionid(s)
    if jsessionid:
        return jsessionid  # 无二次认证，直接成功

    # 3) 是否进入二次认证页（出现 PM1 验证码输入框）
    if 'name="PM1"' in r2.text or "二次认证" in r2.text:
        if code_provider is None:
            raise TwoFactorRequired("账号开启了二次认证，需要动态验证码")
        lt2 = _hidden(r2.text, "lt")
        exec2 = _hidden(r2.text, "execution")
        code = str(code_provider()).strip()
        if not code:
            raise LoginFailed("未输入二次认证验证码")
        r3 = s.post(login_url, params=params, allow_redirects=True, timeout=20, data={
            "PM1": code, "rsa": "", "ul": "", "pl": "",
            "lt": lt2, "execution": exec2, "_eventId": "submit",
        })
        jsessionid = _sdms_jsessionid(s)
        if jsessionid:
            return jsessionid
        if 'name="PM1"' in r3.text:
            raise LoginFailed("二次认证失败（验证码错误或已过期，请重试）")
        raise LoginFailed("二次认证后未获取到会话")

    # 4) 其它情况：停在登录页 = 账号密码有误
    if "cas/login" in r2.url:
        raise LoginFailed("登录未通过（账号或密码有误）")
    raise LoginFailed("登录后未获取到 sdms JSESSIONID")


def _hidden(html, name):
    m = re.search(
        rf'<input[^>]*name=["\']{name}["\'][^>]*value=["\']([^"\']*)["\']', html)
    if m:
        return m.group(1)
    m = re.search(
        rf'<input[^>]*value=["\']([^"\']*)["\'][^>]*name=["\']{name}["\']', html)
    return m.group(1) if m else None


if __name__ == "__main__":
    import sys
    _sid, _pw = sys.argv[1], sys.argv[2]
    print(login(_sid, _pw, code_provider=lambda: input("二次认证验证码: ").strip()))
