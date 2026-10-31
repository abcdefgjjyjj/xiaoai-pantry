"""Voice input from a single OH2P and speech via the household XiaoMusic server."""

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class SpeakerError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never send a Xiaomi cookie or an utterance to another host.


_http = build_opener(ProxyHandler({}), _NoRedirect())
_device_cache = None
last_record_time = None


def _json(url, headers=None, timeout=12):
    try:
        with _http.open(
            Request(url, headers=headers or {}), timeout=timeout
        ) as response:
            return json.load(response)
    except HTTPError as exc:
        raise SpeakerError(f"音箱服务返回 HTTP {exc.code}") from None
    except (OSError, ValueError, URLError) as exc:
        raise SpeakerError(f"音箱服务读取失败（{type(exc).__name__}）") from None


def _target():
    global _device_cache
    base = os.environ.get("XIAOMUSIC_URL", "").strip().rstrip("/")
    if not base:
        raise SpeakerError("未配置 XiaoMusic 服务地址")
    did = os.environ.get("XIAOMUSIC_DID", "").strip()
    key = (base, did)
    if (
        _device_cache
        and _device_cache[0] == key
        and time.monotonic() < _device_cache[1]
    ):
        return _device_cache[2]
    devices = _json(base + "/device_list").get("devices", [])
    matches = [
        d
        for d in devices
        if d.get("hardware") == "OH2P" and (not did or str(d.get("miotDID")) == did)
    ]
    if (
        len(matches) != 1
        or not matches[0].get("deviceID")
        or not matches[0].get("miotDID")
    ):
        raise SpeakerError("无法唯一识别 OH2P 音箱，请在 XIAOMUSIC_DID 指定正确设备")
    target = (base, str(matches[0]["miotDID"]), str(matches[0]["deviceID"]))
    _device_cache = (key, time.monotonic() + 60, target)
    return target


def _cookie(device_id):
    path = Path(os.environ.get("XIAOMUSIC_TOKEN_FILE", "/run/xiaomusic/.mi.token"))
    try:
        with path.open(encoding="utf-8") as file:
            token = json.load(file)
        user, service = token["userId"], token["micoapi"][1]
        if not user or not service:
            raise ValueError("empty")
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        raise SpeakerError(
            "缺少可用的小爱对话凭据，请检查只读挂载和 XiaoMusic 登录状态"
        ) from None
    return f"deviceId={device_id}; serviceToken={service}; userId={user}"


def speak(text):
    """Submit one native play-text action. An ambiguous result is never retried here."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("播报内容不能为空")
    _, did, _ = _target()
    from gateway import cloud_post

    reply = cloud_post(
        "/miotspec/action",
        {
            "params": {"did": did, "siid": 7, "aiid": 3, "in": [text]},
        },
    )
    if (
        not isinstance(reply, dict)
        or str(reply.get("did")) != did
        or reply.get("code") != 0
    ):
        raise SpeakerError("音箱未确认原生播报，暂不重复发送")
    return True


def audibility():
    """Report the OH2P properties without inferring that play-text will be inaudible."""
    _, did, _ = _target()
    from gateway import cloud_post

    properties = {
        (2, 1): "volume",
        (2, 2): "speaker_mute",
        (3, 1): "playing_state",
        (4, 1): "microphone_mute",
        (7, 3): "sleep_mode",
    }
    result = cloud_post(
        "/miotspec/prop/get",
        {
            "params": [
                {"did": did, "siid": siid, "piid": piid} for siid, piid in properties
            ]
        },
    )
    values = dict.fromkeys(properties.values())
    for row in result:
        key = properties.get((row.get("siid"), row.get("piid")))
        if key and str(row.get("did")) == did and row.get("code") == 0:
            values[key] = row.get("value")
    return values


def poll(since_timestamp):
    """Return device-scoped utterances since an inclusive Unix timestamp in seconds."""
    global last_record_time
    _, did, device_id = _target()
    cookie = {"Cookie": _cookie(device_id)}
    since_ms = round(float(since_timestamp) * 1000)
    cursor = None
    found = {}
    while cursor is None or cursor >= since_ms:
        # The official dialog H5 omits timestamp on its live page. Only historical pages
        # carry the server's nextEndTime, including the record at that exact timestamp.
        params = {
            "source": "dialogu",
            "hardware": "OH2P",
            "limit": 10,
            "requestId": secrets.token_hex(12),
        }
        if cursor is not None:
            params["timestamp"] = cursor
        query = urlencode(params)
        reply = _json(
            "https://userprofile.mina.mi.com/device_profile/v2/conversation?" + query,
            cookie,
        )
        if not isinstance(reply, dict) or reply.get("code") != 0:
            raise SpeakerError("小爱对话接口拒绝了当前请求，请检查 XiaoMusic 登录状态")
        try:
            data = json.loads(reply["data"])
            if cursor is None:
                last_record_time = max(
                    (int(r["time"]) / 1000 for r in data["records"]), default=None
                )
            for record in data["records"]:
                ts_ms = int(record["time"])
                utterance = record["query"].strip()
                if ts_ms < since_ms or not utterance or record.get("illegalContent"):
                    continue
                identity = record.get("requestId")
                if not identity:
                    identity = hashlib.sha256(
                        f"{ts_ms}:{utterance}".encode()
                    ).hexdigest()
                record_id = f"{did}:{identity}"
                found[record_id] = {
                    "id": record_id,
                    "text": utterance,
                    "ts": ts_ms / 1000,
                }
            next_cursor = int(data.get("nextEndTime") or 0)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SpeakerError("小爱对话返回格式与当前适配版本不符") from None
        if not next_cursor or not data["records"]:
            break
        if cursor is not None and next_cursor >= cursor:
            raise SpeakerError("小爱对话翻页时间未向前推进")
        cursor = next_cursor
    return sorted(found.values(), key=lambda row: (row["ts"], row["id"]))
