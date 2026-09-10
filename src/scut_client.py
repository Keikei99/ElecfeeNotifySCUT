"""华工水电系统客户端：会话管理 + 绑定房间(room/save) + 查电表余额(ammeterBalance)。

数据流：一个已登录会话可以通过 room/save 把绑定房间切到任意 roomId，
然后 ammeterBalance 读取该房间的实时余额。所有查询必须串行（共用会话绑定态）。
"""
import requests

BASE = "https://dfyc.utc.scut.edu.cn/sdms-weixin-pay"
SERVICE = BASE + "/service"

# 手机端 UA，尽量贴近真实微信/浏览器访问
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


class SessionExpired(Exception):
    """会话失效：接口返回 302 跳转到 CAS/thirdLogin，需要重新登录。"""


class ScutClient:
    def __init__(self, jsessionid=None):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        if jsessionid:
            self.set_session(jsessionid)
        self._user = None  # 缓存 userinfo，供 room/save 复用 realName 等字段

    def set_session(self, jsessionid):
        self.s.cookies.set(
            "JSESSIONID", jsessionid,
            domain="dfyc.utc.scut.edu.cn", path="/sdms-weixin-pay",
        )

    # ---- 内部：GET 并解析 JSON，遇到登录跳转抛 SessionExpired ----
    def _get_json(self, url, **kw):
        r = self.s.get(url, allow_redirects=False, timeout=20, **kw)
        loc = r.headers.get("location", "")
        if r.status_code in (301, 302) and ("thirdLogin" in loc or "cas/login" in loc):
            raise SessionExpired("会话已失效，需要重新登录（更新 JSESSIONID 或启用自动登录）")
        r.raise_for_status()
        return r.json()

    def userinfo(self, refresh=False):
        """获取并缓存当前会话绑定的用户信息（也用于校验会话是否有效）。"""
        if self._user is None or refresh:
            data = self._get_json(SERVICE + "/find/userinfo")
            if str(data.get("statusCode")) != "200":
                raise RuntimeError(f"userinfo 失败: {data.get('message')}")
            self._user = data["resultObject"]
        return self._user

    def set_room(self, loudong_id, louceng_id, room_id):
        """把当前会话的绑定房间切到指定房间。room_save 会复用用户 realName 等字段。"""
        u = self.userinfo()
        payload = {
            "realName": u.get("realName") or "",
            "sex": u.get("sex") or "",
            "tel": u.get("tel") or "",
            "schoolId": str(u.get("schoolId") or ""),
            "campusId": str(u.get("campusId") or "" if u.get("campusId") is not None else ""),
            "loudongId": str(loudong_id),
            "loucengId": str(louceng_id),
            "roomId": str(room_id),
        }
        r = self.s.post(SERVICE + "/room/save", json=payload,
                        allow_redirects=False, timeout=20)
        loc = r.headers.get("location", "")
        if r.status_code in (301, 302) and ("thirdLogin" in loc or "cas/login" in loc):
            raise SessionExpired("会话已失效（room/save 被拦截）")
        r.raise_for_status()
        data = r.json()
        if str(data.get("statusCode")) != "200":
            raise RuntimeError(f"room/save 失败: {data.get('message')}")
        return True

    def balance(self):
        """读取当前绑定房间的电表实时余额。无电表房间返回 None。"""
        data = self._get_json(SERVICE + "/ammeterBalance", params={"type": "1"})
        if str(data.get("statusCode")) != "200":
            return None  # 该房间无电表 / 查询失败
        return data["resultObject"]

    def query_room(self, loudong_id, louceng_id, room_id):
        """便捷方法：切到指定房间并读余额。"""
        self.set_room(loudong_id, louceng_id, room_id)
        return self.balance()
