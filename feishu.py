# -*- coding: utf-8 -*-
"""飞书多维表格（Bitable）封装。

- Token 缓存与自动续期
- 分页读取（search 接口，单页最多 500 条）
- 单条/批量 增、改、删
- upsert：按业务主键存在则更新、不存在则新增
- 返回字段值统一解包：文本 -> str，数字/公式 -> float/int，日期 -> 毫秒时间戳

实测注意：
- 更新记录必须用 PUT（PATCH 会返回 404）
- 应用需要有该多维表格的「可编辑」权限，否则写入返回 403 / code 91403
"""
import logging
import time

import requests

import config as cfg

logger = logging.getLogger(__name__)

BASE = "https://open.feishu.cn/open-apis"

# 可重试的错误码：限流 / 写冲突 / 数据未就绪
RETRY_CODES = {1254290, 1254291, 1254607}


class FeishuError(RuntimeError):
    def __init__(self, code, msg, path=""):
        super().__init__(f"飞书 API 错误 code={code} msg={msg} ({path})")
        self.code = code
        self.msg = msg


def scalar(v):
    """把飞书返回的字段值还原成 Python 标量/字符串。

    兼容：
    - 文本字段：[{"text": "C0", "type": "text"}] 或 "C0"
    - 公式/查找引用：{"type": 2, "value": [2187]} 或 {"value": ["是"]}
    - 单选："蓝色"；多选：["A", "B"]；附件：[{...}]
    """
    if isinstance(v, dict):
        if "value" in v:
            val = v["value"]
            if isinstance(val, list):
                if len(val) == 0:
                    return None
                return scalar(val[0]) if len(val) == 1 else val
            return val
        if "text" in v:
            return v["text"]
        if "name" in v:
            return v["name"]
        return v
    if isinstance(v, list):
        if not v:
            return None
        if all(isinstance(x, dict) and "text" in x for x in v):
            return "".join(str(x["text"]) for x in v)
        if len(v) == 1:
            return scalar(v[0])
        return v
    return v


def as_text(v):
    s = scalar(v)
    if s is None:
        return ""
    if isinstance(s, list):
        return ",".join(str(x) for x in s)
    if isinstance(s, float) and s.is_integer():
        return str(int(s))
    return str(s).strip()


def as_num(v, default=None):
    s = scalar(v)
    if s is None:
        return default
    if isinstance(s, bool):
        return float(s)
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "nan", "None", "null"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def as_ms(v):
    """日期字段统一转成毫秒时间戳（int）。"""
    s = scalar(v)
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return int(s)
    try:
        ts = __import__("pandas").Timestamp(s)
        return int(ts.timestamp() * 1000)
    except Exception:
        return None


def ms_to_day(ms):
    """飞书日期字段（UTC 毫秒）-> 本地日期（naive Timestamp，当天 00:00）。

    飞书日期存的是「本地零点对应的 UTC 时刻」，直接按 UTC 取日期会早一天。
    """
    import pandas as pd
    return (pd.Timestamp(int(ms), unit="ms", tz="UTC")
            .tz_convert(cfg.LOCAL_TZ).tz_localize(None).normalize())


def day_to_ms(day):
    """本地日期 -> 飞书日期字段需要的毫秒时间戳。"""
    import pandas as pd
    ts = pd.Timestamp(day)
    if ts.tzinfo is None:
        ts = ts.tz_localize(cfg.LOCAL_TZ)
    return int(ts.timestamp() * 1000)


def today():
    """本地时区的今天（naive Timestamp，00:00）。"""
    import pandas as pd
    return pd.Timestamp.now(tz=cfg.LOCAL_TZ).tz_localize(None).normalize()


def now_ms():
    """当前时刻的毫秒时间戳（真实 epoch）。

    注意：不要用 pd.Timestamp.now().timestamp()——pandas 把 naive 时间当作 UTC，
    在 UTC+8 的机器上会多出 8 小时。
    """
    return int(time.time() * 1000)


def ms_to_str(ms):
    """毫秒时间戳 -> 本地时间字符串（用于 CSV / 日志）。"""
    import pandas as pd
    return (pd.Timestamp(int(ms), unit="ms", tz="UTC")
            .tz_convert(cfg.LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"))


class Feishu:
    def __init__(self, app_id=None, app_secret=None, app_token=None):
        self.app_id = app_id or cfg.APP_ID
        self.app_secret = app_secret or cfg.APP_SECRET
        self.app_token = app_token or cfg.APP_TOKEN
        if not self.app_secret:
            raise RuntimeError(
                "缺少 FEISHU_APP_SECRET：请在项目目录建 .env 写入 "
                "FEISHU_APP_SECRET=xxx，或设置同名环境变量（GitHub Actions 用 Secrets）")
        self._token = None
        self._expire = 0.0
        self._sess = requests.Session()
        self._last_req = 0.0
        # 飞书多维表格默认限制约 20 QPS，这里主动降速，避免 429
        self.min_interval = 0.06

    # ------------------------------------------------------------------ 基础
    def _throttle(self):
        gap = time.time() - self._last_req
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_req = time.time()

    def _get_token(self, force=False):
        if self._token and not force and time.time() < self._expire:
            return self._token
        resp = self._sess.post(
            f"{BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(data.get("code"), data.get("msg"), "auth")
        self._token = data["tenant_access_token"]
        # 提前 60 秒过期，避免边界情况
        self._expire = time.time() + max(int(data.get("expire", 7200)) - 60, 60)
        return self._token

    def _request(self, method, path, body=None, params=None, retries=5, timeout=25):
        """发起请求并解包 data；失败按限流/网络错误重试。"""
        url = path if path.startswith("http") else BASE + path
        last_err = None
        for attempt in range(retries):
            self._throttle()
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            try:
                resp = self._sess.request(method, url, headers=headers, json=body,
                                          params=params, timeout=timeout)
            except requests.RequestException as e:
                last_err = e
                time.sleep(min(2 ** attempt, 8))
                continue

            if resp.status_code == 401:
                # token 失效，强制刷新后重试
                self._get_token(force=True)
                last_err = FeishuError(401, "token 失效", path)
                continue

            try:
                data = resp.json()
            except ValueError:
                raise FeishuError(resp.status_code, resp.text[:200], path)

            code = data.get("code", -1)
            if code == 0:
                return data.get("data")
            if code in RETRY_CODES or resp.status_code == 429 or resp.status_code >= 500:
                last_err = FeishuError(code, data.get("msg"), path)
                time.sleep(min(2 ** attempt, 8))
                continue
            raise FeishuError(code, data.get("msg"), path)
        raise last_err if last_err else FeishuError(-1, "请求失败", path)

    # ------------------------------------------------------------------ 读
    def list_records(self, table_id, page_size=500, field_names=None):
        """读取整张表（自动翻页），返回原始 items。"""
        items = []
        page_token = None
        while True:
            body = {"page_size": page_size}
            if field_names:
                body["field_names"] = list(field_names)
            if page_token:
                body["page_token"] = page_token
            data = self._request(
                "POST",
                f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/search",
                body=body,
            ) or {}
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        return items

    def read_table(self, table_id, page_size=500, field_names=None):
        """读取整张表并把字段值解包，返回 [{'_id': record_id, 字段: 值}]。"""
        rows = []
        for item in self.list_records(table_id, page_size=page_size, field_names=field_names):
            fields = {k: scalar(v) for k, v in (item.get("fields") or {}).items()}
            fields["_id"] = item.get("record_id")
            rows.append(fields)
        return rows

    def read(self, table_name):
        """按 config.TABLES 的表名读取。"""
        if table_name not in cfg.TABLES:
            raise ValueError(f"未知表名 {table_name}，可选：{list(cfg.TABLES)}")
        return self.read_table(cfg.TABLES[table_name])

    def search(self, table_id, body):
        """直接调用 records/search，返回 data（含 items/total/has_more）。"""
        return self._request(
            "POST", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/search",
            body=body) or {}

    def count(self, table_id, filter_obj=None):
        """按条件统计记录数（只取一页，用 total 字段）。"""
        body = {"page_size": 1}
        if filter_obj:
            body["filter"] = filter_obj
        return int(self.search(table_id, body).get("total", 0) or 0)

    def find_ids(self, table_id, filter_obj=None, page_size=500):
        """按条件取出所有 record_id（自动翻页）。"""
        ids, token = [], None
        while True:
            body = {"page_size": page_size}
            if filter_obj:
                body["filter"] = filter_obj
            if token:
                body["page_token"] = token
            data = self.search(table_id, body)
            ids.extend(x.get("record_id") for x in (data.get("items") or []))
            if not data.get("has_more"):
                break
            token = data.get("page_token")
            if not token:
                break
        return ids

    # ------------------------------------------------------------------ 写
    def create(self, table_id, fields):
        data = self._request(
            "POST", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records",
            body={"fields": fields},
        ) or {}
        return (data.get("record") or {}).get("record_id")

    def batch_create(self, table_id, rows, chunk=200):
        """批量新增，返回新建 record_id 列表。rows 为 fields 字典列表。"""
        ids = []
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            data = self._request(
                "POST", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_create",
                body={"records": [{"fields": r} for r in part]},
            ) or {}
            ids.extend(x.get("record_id") for x in (data.get("records") or []))
        return ids

    def update(self, table_id, record_id, fields):
        """更新单条记录（必须用 PUT）。"""
        return self._request(
            "PUT", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/{record_id}",
            body={"fields": fields},
        )

    def batch_update(self, table_id, updates, chunk=200):
        """批量更新。updates = [{'record_id': ..., 'fields': {...}}]"""
        for i in range(0, len(updates), chunk):
            self._request(
                "POST", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_update",
                body={"records": updates[i:i + chunk]},
            )

    def delete(self, table_id, record_id):
        return self._request(
            "DELETE", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/{record_id}"
        )

    def batch_delete(self, table_id, record_ids, chunk=200):
        # 注意：batch_delete 的 records 是 record_id 字符串数组，不是对象数组
        for i in range(0, len(record_ids), chunk):
            self._request(
                "POST", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_delete",
                body={"records": list(record_ids[i:i + chunk])},
            )

    # ------------------------------------------------------------------ upsert
    def upsert(self, table_id, key_field, rows, key_func=None):
        """按 key_field 做 upsert。

        返回 (created, updated) 计数。
        key_func(row) 可自定义比较键（例如日期按天比较），默认用 as_text 归一化。
        """
        existing = {}
        for r in self.read_table(table_id):
            k = key_func(r) if key_func else as_text(r.get(key_field))
            if k not in (None, ""):
                existing[k] = r["_id"]

        to_create, to_update = [], []
        for row in rows:
            k = key_func(row) if key_func else as_text(row.get(key_field))
            rid = existing.get(k)
            if rid:
                payload = {kk: vv for kk, vv in row.items() if kk != "_id"}
                to_update.append({"record_id": rid, "fields": payload})
            else:
                to_create.append({kk: vv for kk, vv in row.items() if kk != "_id"})

        if to_create:
            self.batch_create(table_id, to_create)
        if to_update:
            self.batch_update(table_id, to_update)
        logger.info(f"upsert {table_id}: 新增 {len(to_create)} 条，更新 {len(to_update)} 条")
        return len(to_create), len(to_update)
