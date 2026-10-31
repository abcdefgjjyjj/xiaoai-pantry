"""Read kitchen presence from the existing Xiaomi Home account, without Home Assistant."""

import base64
import hashlib
import hmac
import http.client
import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path


MODEL = "xiaomi.sensor_occupy.03"
SENSOR_DID = os.environ.get("XIAOAI_KITCHEN_DID")
TOKEN_FILE = Path(os.environ.get("XIAOAI_MI_TOKEN_FILE", "/run/xiaomusic/.mi.token"))
# This sensor sends a new event at the start, after 2 minutes, and after 5 minutes.
MAX_AGE = 180


@dataclass(frozen=True)
class PresenceSample:
    occupied: bool | None
    updated_at: int | None
    last_state: str
    reason: str
    source: str = "xiaomi.sensor_occupy.03"
    duration_minutes: int = 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _MiCloud:
    def __init__(self):
        self.credentials = None
        self.did = None
        self.cookies = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPCookieProcessor(self.cookies)
        )

    def login(self):
        # Read an existing session; never replace or clear the source file.
        token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        cookies = "; ".join(f"{key}={token[key]}" for key in ("userId", "deviceId", "passToken"))
        req = urllib.request.Request(
            "https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true",
            headers={"User-Agent": "Android", "Cookie": cookies},
        )
        self.cookies.clear()
        with self.http.open(req, timeout=6) as response:
            login = json.loads(response.read().removeprefix(b"&&&START&&&"))
        if login.get("code") != 0:
            raise ValueError("session_expired")
        location = login["location"]
        callback = urllib.parse.urlsplit(location)
        if callback.scheme != "https" or callback.hostname != "sts.api.io.mi.com":
            raise ValueError("unexpected_login_callback")
        security = login["ssecurity"]
        sign = hashlib.sha1(f"nonce={login['nonce']}&{security}".encode()).digest()
        location += "&" + urllib.parse.urlencode({"clientSign": base64.b64encode(sign)})
        try:
            with self.http.open(location, timeout=6) as response:
                response.read(1)
        except urllib.error.HTTPError as error:
            # Some account service versions return their cookie on a redirect.
            if error.code not in (301, 302, 303, 307, 308):
                raise
        service_token = next(c.value for c in self.cookies if c.name == "serviceToken")
        self.credentials = (str(login["userId"]), token["deviceId"], service_token, security)

    def post(self, path, params):
        if self.credentials is None:
            self.login()
        user, device, session, security = self.credentials
        data = json.dumps(params, ensure_ascii=True, separators=(",", ":"))
        nonce = os.urandom(8) + int(time.time() // 60).to_bytes(4, "big")
        b64nonce = base64.b64encode(nonce).decode()
        signed = hashlib.sha256(base64.b64decode(security) + nonce).digest()
        material = "&".join([path, base64.b64encode(signed).decode(), b64nonce, "data=" + data])
        signature = base64.b64encode(hmac.digest(signed, material.encode(), "sha256")).decode()
        form = urllib.parse.urlencode({"data": data, "_nonce": b64nonce, "signature": signature})
        req = urllib.request.Request(
            "https://api.io.mi.com/app" + path,
            data=form.encode(),
            headers={
                "Cookie": f"userId={user}; serviceToken={session}; PassportDeviceId={device}",
                "User-Agent": "Android",
                "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            },
        )
        try:
            with self.http.open(req, timeout=6) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                self.credentials = None
            raise
        if result.get("code") != 0:
            if result.get("code") in (-3, 401, 403) or "auth" in str(result.get("message", "")).lower():
                self.credentials = None
            raise ValueError(f"cloud_error_{result.get('code')}")
        return result["result"]

    def sample(self):
        if not self.did:
            devices = self.post("/home/device_list", {"getVirtualModel": False, "getHuamiDevices": 0})["list"]
            matches = [d["did"] for d in devices if d.get("model") == MODEL
                       and (not SENSOR_DID or d.get("did") == SENSOR_DID)]
            if len(matches) != 1:
                raise ValueError("kitchen_sensor_not_unique")
            self.did = matches[0]
        ids = (1078, 1081, 1082)
        rows = self.post("/miotspec/prop/get", {
            "params": [{"did": self.did, "siid": 2, "piid": prop} for prop in ids]
        })
        return interpret([r for r in rows if r.get("did") == self.did], time.time())


def interpret(rows, now):
    """Use the sensor timestamps, including duration reports which renew an occupied state."""
    props = {r["piid"]: r for r in rows if r.get("code") == 0 and r.get("siid") == 2
             and type(r.get("updateTime")) is int and type(r.get("value")) is int}
    status = props.get(1078, {})
    value = status.get("value")
    if value not in (0, 1):
        return PresenceSample(None, status.get("updateTime"), "unknown", "no_valid_status")
    occupied = bool(value)
    stamp = status["updateTime"]
    duration = 0
    for prop, state, durations in [(1081, True, (2, 5)), (1082, False, (2, 5, 10, 30))]:
        row = props.get(prop, {})
        if row.get("value") not in durations or row["updateTime"] < stamp:
            continue
        if state != occupied:
            return PresenceSample(None, row["updateTime"], "unknown", "conflicting_reports")
        stamp = row["updateTime"]
        if occupied:
            duration = row["value"]
    last = "occupied" if occupied else "vacant"
    if not (0 <= now - stamp <= MAX_AGE):
        return PresenceSample(None, stamp, last, "stale_or_future")
    return PresenceSample(occupied, stamp, last, "fresh", duration_minutes=duration)


_cloud = _MiCloud()
_lock = threading.Lock()
_cached = None
_check_after = 0.0


def read_sample():
    """Return a safe diagnostic sample; errors contain no account data or credentials."""
    global _cached, _check_after
    with _lock:
        if _cached is not None and time.monotonic() < _check_after:
            if _cached.occupied is not None and not (0 <= time.time() - _cached.updated_at <= MAX_AGE):
                return PresenceSample(None, _cached.updated_at, _cached.last_state, "stale_or_future")
            return _cached
        try:
            _cached = _cloud.sample()
        except (OSError, ValueError, KeyError, StopIteration, TypeError, http.client.HTTPException) as error:
            reason = str(error) if type(error) is ValueError and str(error) in (
                "kitchen_sensor_not_unique", "session_expired", "unexpected_login_callback"
            ) else type(error).__name__
            _cached = PresenceSample(None, None, "unknown", reason)
        _check_after = time.monotonic() + (30 if _cached.updated_at is None else 10)
        return _cached


def read_presence():
    return read_sample().occupied


def cloud_post(path, params):
    """Send one signed request with the shared session. Never replay an action after errors."""
    with _lock:
        return _cloud.post(path, params)


if __name__ == "__main__":
    print(json.dumps(asdict(read_sample()), ensure_ascii=False))
