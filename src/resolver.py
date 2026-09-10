"""房间解析：把「楼栋 + 房间号」解析成系统内部的 (loudongId, loucengId, roomId)。

用的是完全公开、无需登录的地理级联接口（带固定参数 idCode=1001）：
  school_loudong/list -> louceng/list -> louceng_room/list
结果会缓存到项目的 json/ 目录，避免每次都请求。
"""
import json
import os
import requests

SERVICE = "https://dfyc.utc.scut.edu.cn/sdms-weixin-pay/service"
SCHOOL_ID = 5033  # 华南理工大学城校区（可按需扩展多校区）
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# 预爬好的「房间全名 -> {roomId, loucengId, loudongId, loudongName}」映射，
# 由 src/crawl_rooms.py 生成。存在时优先用它，避免每次都实时请求。
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
JSON_DIR = os.path.join(PROJECT_ROOT, "json")
MAP_PATH = os.path.join(JSON_DIR, "rooms_map.json")


class RoomResolver:
    def __init__(self, cache_path=None):
        self.cache_path = cache_path or os.path.join(JSON_DIR, "rooms_cache.json")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.cache = {"buildings": None, "floors": {}, "rooms": {}}
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception:
                pass
        self._map = None  # 归一化名 -> 房间信息（懒加载 json/rooms_map.json）

    def _save(self):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)

    def _get(self, path, **params):
        params["idCode"] = "1001"
        r = self.s.get(SERVICE + path, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if str(data.get("statusCode")) != "200":
            raise RuntimeError(f"{path} 失败: {data.get('message')}")
        return data["resultObject"]

    # ---- 三级级联（带缓存） ----
    def buildings(self):
        if not self.cache.get("buildings"):
            self.cache["buildings"] = self._get(
                "/school_loudong/list", schoolId=SCHOOL_ID)
            self._save()
        return self.cache["buildings"]

    def floors(self, loudong_id):
        key = str(loudong_id)
        if key not in self.cache["floors"]:
            self.cache["floors"][key] = self._get(
                "/louceng/list", loudongId=loudong_id)
            self._save()
        return self.cache["floors"][key]

    def rooms(self, louceng_id):
        key = str(louceng_id)
        if key not in self.cache["rooms"]:
            self.cache["rooms"][key] = self._get(
                "/louceng_room/list", loucengId=louceng_id)
            self._save()
        return self.cache["rooms"][key]

    # ---- 名称归一化 ----
    @staticmethod
    def _norm(s):
        return str(s).strip().upper().replace(" ", "").replace("栋", "").replace("-", "")

    def _find_building(self, building):
        target = self._norm(building)
        for b in self.buildings():
            if self._norm(b["loudongName"]) == target:
                return b
        # 宽松匹配（包含关系）
        for b in self.buildings():
            if target in self._norm(b["loudongName"]):
                return b
        raise LookupError(f"找不到楼栋: {building}")

    def resolve(self, building, room):
        """返回 dict: loudongId, loucengId, roomId, roomName, loudongName。

        building 例: "C1" / "C1栋"；room 例: "101" / "C1-101"。
        """
        b = self._find_building(building)
        loudong_id = b["loudongId"]
        loudong_name = b["loudongName"]

        # 目标房间全名，如 "C1-101"
        room_str = str(room).strip().upper()
        b_prefix = loudong_name.replace("栋", "")
        if "-" in room_str or b_prefix in room_str:
            target_room = self._norm(room_str)
        else:
            target_room = self._norm(f"{b_prefix}-{room_str}")

        floors = self.floors(loudong_id)

        # 优先按房间号推断楼层（如 208 -> 2 层），命中失败再全楼扫描
        ordered = list(floors)
        digits = "".join(ch for ch in room_str if ch.isdigit())
        if len(digits) >= 3:
            floor_num = str(int(digits) // 100)
            guessed = [f for f in floors
                       if self._norm(f["loucengName"]) == self._norm(f"{b_prefix}-{floor_num}")]
            others = [f for f in floors if f not in guessed]
            ordered = guessed + others

        for f in ordered:
            for r in self.rooms(f["loucengId"]):
                if self._norm(r["roomName"]) == target_room:
                    return {
                        "loudongId": loudong_id,
                        "loucengId": f["loucengId"],
                        "roomId": r["roomId"],
                        "roomName": r["roomName"],
                        "loudongName": loudong_name,
                    }
        raise LookupError(f"找不到房间: {building} {room}")

    # ---- 按完整房间号解析（订阅用），优先查预爬的 json/rooms_map.json ----
    def _room_map(self):
        if self._map is None:
            self._map = {}
            if os.path.exists(MAP_PATH):
                try:
                    with open(MAP_PATH, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    for name, info in raw.items():
                        self._map[self._norm(name)] = {**info, "roomName": name}
                except Exception:
                    self._map = {}
        return self._map

    def resolve_full(self, room_name):
        """输入完整房间号（如 "C1-101"）→ (loudongId, loucengId, roomId, ...)。

        优先命中预爬映射 json/rooms_map.json；未命中则按楼栋拆分实时解析。
        """
        m = self._room_map()
        hit = m.get(self._norm(room_name))
        if hit:
            return {
                "loudongId": hit["loudongId"],
                "loucengId": hit["loucengId"],
                "roomId": hit["roomId"],
                "roomName": hit.get("roomName", room_name),
                "loudongName": hit.get("loudongName", ""),
            }
        s = str(room_name).strip()
        if "-" not in s:
            raise LookupError(f"房间号需形如 C1-101：{room_name}")
        building, room = s.split("-", 1)
        return self.resolve(building, room)
