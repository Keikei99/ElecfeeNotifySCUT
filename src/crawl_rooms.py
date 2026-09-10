#!/usr/bin/env python3
"""爬取楼栋内所有房间的 roomId，写入 json/rooms_map.json（供订阅解析用）。

数据来自公开、免登录的地理级联接口（school_loudong / louceng / louceng_room，
带固定参数 idCode=1001）。roomId 是按“楼层”批量返回的，一次请求即可拿到一层的全部
房间，因此请求次数不多；为稳妥起见，**每次请求之间随机间隔 5~6 秒**。

用法:
  python3 src/crawl_rooms.py                 # 默认爬 C1
  python3 src/crawl_rooms.py C1 C2 C3        # 爬指定楼栋（增量合并进 json/rooms_map.json）
  python3 src/crawl_rooms.py --all           # 爬城校区所有楼栋
  python3 src/crawl_rooms.py --list          # 只列出所有楼栋名，不爬

输出 json/rooms_map.json 结构（按房间全名索引）:
  { "C1-101": {"roomId":9356, "loucengId":9348, "loudongId":9306, "loudongName":"C1栋"}, ... }
"""
import argparse
import json
import os
import random
import sys
import time

import requests

from resolver import SERVICE, SCHOOL_ID, UA, MAP_PATH

MIN_DELAY, MAX_DELAY = 5.0, 6.0


def _norm(s):
    return str(s).strip().upper().replace(" ", "").replace("栋", "").replace("-", "")


class Crawler:
    def __init__(self, delay_range=(MIN_DELAY, MAX_DELAY)):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.delay_range = delay_range
        self._first = True

    def _get(self, path, **params):
        # 请求间随机节流（首个请求不等待）
        if not self._first:
            d = random.uniform(*self.delay_range)
            time.sleep(d)
        self._first = False
        params["idCode"] = "1001"
        r = self.s.get(SERVICE + path, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if str(data.get("statusCode")) != "200":
            raise RuntimeError(f"{path} 失败: {data.get('message')}")
        return data["resultObject"]

    def buildings(self):
        return self._get("/school_loudong/list", schoolId=SCHOOL_ID)

    def floors(self, loudong_id):
        return self._get("/louceng/list", loudongId=loudong_id)

    def rooms_of_floor(self, louceng_id):
        return self._get("/louceng_room/list", loucengId=louceng_id)

    def rooms_of_building(self, loudong_id):
        # 少数楼栋没有楼层，直接挂房间
        return self._get("/loudong_room/list", loudongId=loudong_id)


def load_map():
    if os.path.exists(MAP_PATH):
        with open(MAP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_map(m):
    os.makedirs(os.path.dirname(MAP_PATH), exist_ok=True)
    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def crawl(building_names=None, crawl_all=False):
    c = Crawler()
    print("获取楼栋列表……")
    all_buildings = c.buildings()  # [{loudongId, loudongName}]
    by_norm = {_norm(b["loudongName"]): b for b in all_buildings}

    if crawl_all:
        targets = all_buildings
    else:
        targets = []
        for name in building_names:
            b = by_norm.get(_norm(name))
            if not b:
                print(f"  ⚠️ 找不到楼栋：{name}（跳过）")
                continue
            targets.append(b)
    if not targets:
        print("没有可爬的楼栋。用 --list 查看所有楼栋名。")
        return

    room_map = load_map()
    total_rooms = 0
    for b in targets:
        lid, lname = b["loudongId"], b["loudongName"]
        print(f"\n=== {lname} (loudongId={lid}) ===")
        floors = c.floors(lid)
        if floors:
            for f in floors:
                rooms = c.rooms_of_floor(f["loucengId"])
                for r in rooms:
                    room_map[r["roomName"]] = {
                        "roomId": r["roomId"],
                        "loucengId": f["loucengId"],
                        "loudongId": lid,
                        "loudongName": lname,
                    }
                total_rooms += len(rooms)
                print(f"  {f['loucengName']}: {len(rooms)} 间")
                save_map(room_map)  # 边爬边存，中断也不丢
        else:
            rooms = c.rooms_of_building(lid)
            for r in rooms:
                room_map[r["roomName"]] = {
                    "roomId": r["roomId"],
                    "loucengId": "",
                    "loudongId": lid,
                    "loudongName": lname,
                }
            total_rooms += len(rooms)
            print(f"  （无楼层）直接房间: {len(rooms)} 间")
            save_map(room_map)

    print(f"\n完成：本次涉及 {len(targets)} 栋，累计 {total_rooms} 间，"
          f"映射表共 {len(room_map)} 条，已写入 {MAP_PATH}")


def main():
    ap = argparse.ArgumentParser(description="爬取楼栋房间 roomId 到 json/rooms_map.json")
    ap.add_argument("buildings", nargs="*", help="要爬的楼栋名，如 C1 C2；缺省爬 C1")
    ap.add_argument("--all", action="store_true", help="爬城校区所有楼栋")
    ap.add_argument("--list", action="store_true", help="只列出所有楼栋名")
    args = ap.parse_args()

    if args.list:
        for b in Crawler().buildings():
            print(f"{b['loudongName']}  (loudongId={b['loudongId']})")
        return 0

    names = args.buildings or ["C1"]
    crawl(building_names=names, crawl_all=args.all)
    return 0


if __name__ == "__main__":
    sys.exit(main())
