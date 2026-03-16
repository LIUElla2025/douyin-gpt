"""Vercel Serverless API — 抖音视频文字稿提取

Flask 应用，提供以下 API：
- POST /api/resolve-url    解析抖音链接 → sec_uid
- POST /api/fetch-videos   获取博主视频列表
- POST /api/transcribe     转录单个视频音频
- POST /api/generate-doc   生成 Word 文档
- POST /api/chat           博主 GPT 对话
"""

import base64
import hashlib
import json
import os
import random
import re
import string
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

# ─── 从 Vercel 环境变量读取默认配置 ───
_ENV_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
_ENV_COOKIE = os.environ.get("DOUYIN_COOKIE", "")
_ENV_APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN", "")
_ENV_PROXY = os.environ.get("PROXY", "")


def _clean_cookie(cookie: str) -> str:
    """清理 Cookie 中的非法字符（换行、回车等）"""
    return re.sub(r'[\r\n\t]+', '', cookie).strip()


def _get_config(data: dict) -> dict:
    """合并请求参数和环境变量，环境变量作为默认值"""
    return {
        "openai_api_key": data.get("openai_api_key", "").strip() or _ENV_OPENAI_KEY,
        "cookie": _clean_cookie(data.get("cookie", "") or _ENV_COOKIE),
        "apify_token": data.get("apify_token", "").strip() or _ENV_APIFY_TOKEN,
        "proxy": data.get("proxy", "").strip() or _ENV_PROXY,
    }


# ─── CORS ───


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/api/<path:path>", methods=["OPTIONS"])
def handle_options(path):
    return "", 204


# ─── 健康检查 ───


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


@app.route("/api/config-status", methods=["GET"])
def config_status():
    """返回服务端已配置哪些 Key（不暴露值，只返回 bool）"""
    return jsonify({
        "has_openai_key": bool(_ENV_OPENAI_KEY),
        "has_cookie": bool(_ENV_COOKIE),
        "has_apify_token": bool(_ENV_APIFY_TOKEN),
        "has_proxy": bool(_ENV_PROXY),
    })


# ─── 解析抖音链接 ───


@app.route("/api/resolve-url", methods=["POST"])
def resolve_url():
    data = request.json or {}
    user_input = data.get("input", "").strip()
    if not user_input:
        return jsonify({"error": "请输入抖音链接或抖音号"}), 400

    try:
        profile_url = _resolve_douyin_input(user_input)
        sec_uid = _extract_sec_uid(profile_url)
        return jsonify({"profile_url": profile_url, "sec_uid": sec_uid})
    except Exception as e:
        return jsonify({"error": f"解析失败: {e}"}), 400


# ─── 获取视频列表 ───


@app.route("/api/fetch-videos", methods=["POST"])
def fetch_videos():
    data = request.json or {}
    cfg = _get_config(data)
    sec_uid = data.get("sec_uid", "").strip()
    cookie = cfg["cookie"]
    keyword = data.get("keyword", "").strip()
    max_videos = data.get("max_videos", 0)
    use_apify = data.get("use_apify", False)
    apify_token = cfg["apify_token"]

    if not sec_uid:
        return jsonify({"error": "缺少 sec_uid"}), 400

    # 优先直接调用抖音 API（轻量级，无 f2 依赖）
    if not use_apify and cookie:
        try:
            videos, creator_name = _fetch_videos_direct(
                sec_uid, cookie, max_videos, keyword
            )
            return jsonify({
                "videos": videos,
                "creator_name": creator_name,
                "total": len(videos),
                "method": "direct",
            })
        except Exception as e:
            if not apify_token:
                return jsonify({"error": f"获取失败: {e}"}), 500

    # Apify 备选
    if apify_token:
        try:
            profile_url = f"https://www.douyin.com/user/{sec_uid}"
            videos = _apify_fetch_videos(apify_token, profile_url, max_videos or 200)
            creator_name = videos[0].get("author", "") if videos else ""
            if keyword:
                keywords = keyword.split()
                videos = [v for v in videos if _match_keyword(v, keywords)]
            return jsonify({
                "videos": videos,
                "creator_name": creator_name,
                "total": len(videos),
                "method": "apify",
            })
        except Exception as e:
            return jsonify({"error": f"Apify 获取失败: {e}"}), 500

    return jsonify({"error": "请提供抖音 Cookie 或 Apify Token"}), 400


# ─── 转录单个视频 ───


@app.route("/api/transcribe", methods=["POST"])
def transcribe():
    data = request.json or {}
    cfg = _get_config(data)
    audio_url = data.get("audio_url", "").strip()
    video_url = data.get("video_url", "").strip()
    openai_key = cfg["openai_api_key"]
    proxy = cfg["proxy"]

    if not openai_key:
        return jsonify({"error": "缺少 OpenAI API Key"}), 400

    download_url = audio_url or video_url
    if not download_url:
        return jsonify({"error": "缺少音频/视频 URL"}), 400

    try:
        # 下载音频到临时文件
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_path = tmp.name
        tmp.close()

        try:
            req = urllib.request.Request(download_url)
            req.add_header("User-Agent", "Mozilla/5.0")
            resp = urllib.request.urlopen(req, timeout=60)
            with open(tmp_path, "wb") as f:
                f.write(resp.read())

            file_size = os.path.getsize(tmp_path)
            if file_size < 1000:
                return jsonify({"error": "音频文件太小，可能下载失败"}), 400

            # 调用 Whisper API
            transcript = _call_whisper(tmp_path, openai_key, proxy)
            return jsonify({"transcript": transcript})

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    except Exception as e:
        return jsonify({"error": f"转录失败: {e}"}), 500


# ─── 生成 Word 文档 ───


@app.route("/api/generate-doc", methods=["POST"])
def generate_doc():
    data = request.json or {}
    videos = data.get("videos", [])
    creator_name = data.get("creator_name", "博主")

    if not videos:
        return jsonify({"error": "没有视频数据"}), 400

    try:
        doc_bytes = _generate_word_doc(videos, creator_name)
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", creator_name)
        filename = f"{safe_name}_文字稿合集.docx"

        return send_file(
            BytesIO(doc_bytes),
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        return jsonify({"error": f"文档生成失败: {e}"}), 500


# ─── 博主 GPT 对话 ───


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    cfg = _get_config(data)
    message = data.get("message", "").strip()
    creator_name = data.get("creator_name", "博主")
    videos_context = data.get("videos_context", [])
    history = data.get("history", [])
    openai_key = cfg["openai_api_key"]
    proxy = cfg["proxy"]

    if not message:
        return jsonify({"error": "消息不能为空"}), 400
    if not openai_key:
        return jsonify({"error": "缺少 OpenAI API Key"}), 400

    try:
        reply = _chat_with_creator(
            message, creator_name, videos_context, history, openai_key, proxy
        )
        return jsonify({"response": reply})
    except Exception as e:
        return jsonify({"error": f"对话失败: {e}"}), 500


# ═══════════════════════════════════════════════════
# 内部实现函数
# ═══════════════════════════════════════════════════


def _resolve_douyin_input(user_input: str) -> str:
    """解析用户输入为抖音主页 URL"""
    user_input = user_input.strip()

    # 短链接
    if "v.douyin.com" in user_input or "douyin.com/share" in user_input:
        url_match = re.search(r"https?://[^\s]+", user_input)
        if url_match:
            short_url = url_match.group(0)
            try:
                req = urllib.request.Request(short_url, method="HEAD")
                req.add_header("User-Agent", "Mozilla/5.0")
                resp = urllib.request.urlopen(req, timeout=10)
                final_url = resp.url
                sec_uid_match = re.search(r"sec_uid=([^&]+)", final_url)
                if sec_uid_match:
                    sec_uid = urllib.parse.unquote(sec_uid_match.group(1))
                    return f"https://www.douyin.com/user/{sec_uid}"
                user_match = re.search(r"/user/([^?&]+)", final_url)
                if user_match:
                    return f"https://www.douyin.com/user/{user_match.group(1)}"
            except Exception:
                pass

    # 完整 URL
    if "douyin.com/user/" in user_input:
        user_match = re.search(r"douyin\.com/user/([^?&\s]+)", user_input)
        if user_match:
            return f"https://www.douyin.com/user/{user_match.group(1)}"

    # 纯 sec_uid
    if user_input.startswith("MS4wLjABAAAA"):
        return f"https://www.douyin.com/user/{user_input}"

    return f"https://www.douyin.com/user/{user_input}"


def _extract_sec_uid(profile_url: str) -> str:
    """从主页 URL 提取 sec_uid"""
    match = re.search(r"/user/([^?&\s]+)", profile_url)
    return match.group(1) if match else ""


def _match_keyword(video: dict, keywords: list[str]) -> bool:
    """检查视频是否匹配关键词"""
    title = video.get("title", "") or ""
    raw_title = video.get("raw_title", "") or ""
    return any(kw in title or kw in raw_title for kw in keywords)


# ─── XBogus 签名（纯 Python，无外部依赖）───


class _XBogus:
    """抖音 X-Bogus 签名算法（来自 f2 项目，Apache 2.0 协议）"""

    def __init__(self, user_agent: str = "") -> None:
        self.Array = [
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
            0, 1, 2, 3, 4, 5, 6, 7, 8, 9, None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None, None, 10, 11, 12, 13, 14, 15
        ]
        self.character = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
        self.ua_key = b"\x00\x01\x0c"
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
        )

    def _md5_str_to_array(self, md5_str):
        if isinstance(md5_str, str) and len(md5_str) > 32:
            return [ord(c) for c in md5_str]
        array, idx = [], 0
        while idx < len(md5_str):
            array.append((self.Array[ord(md5_str[idx])] << 4) | self.Array[ord(md5_str[idx + 1])])
            idx += 2
        return array

    def _md5(self, input_data):
        if isinstance(input_data, str):
            arr = self._md5_str_to_array(input_data)
        else:
            arr = input_data
        return hashlib.md5(bytes(arr)).hexdigest()

    def _rc4_encrypt(self, key, data):
        S = list(range(256))
        j = 0
        for i in range(256):
            j = (j + S[i] + key[i % len(key)]) % 256
            S[i], S[j] = S[j], S[i]
        i = j = 0
        out = bytearray()
        for byte in data:
            i = (i + 1) % 256
            j = (j + S[i]) % 256
            S[i], S[j] = S[j], S[i]
            out.append(byte ^ S[(S[i] + S[j]) % 256])
        return out

    def _calc(self, a1, a2, a3):
        x3 = ((a1 & 255) << 16) | ((a2 & 255) << 8) | a3
        return (self.character[(x3 & 16515072) >> 18] + self.character[(x3 & 258048) >> 12]
                + self.character[(x3 & 4032) >> 6] + self.character[x3 & 63])

    def get_xbogus(self, url_params):
        a1 = self._md5_str_to_array(self._md5(
            base64.b64encode(self._rc4_encrypt(self.ua_key, self.user_agent.encode("ISO-8859-1"))).decode("ISO-8859-1")
        ))
        a2 = self._md5_str_to_array(self._md5(self._md5_str_to_array("d41d8cd98f00b204e9800998ecf8427e")))
        up = self._md5_str_to_array(self._md5(self._md5_str_to_array(self._md5(url_params))))
        timer = int(time.time())
        ct = 536919696
        na = [64, 0, 1, 12, up[14], up[15], a2[14], a2[15], a1[14], a1[15],
              timer >> 24 & 255, timer >> 16 & 255, timer >> 8 & 255, timer & 255,
              ct >> 24 & 255, ct >> 16 & 255, ct >> 8 & 255, ct & 255]
        xor_r = na[0]
        for v in na[1:]:
            xor_r ^= int(v)
        na.append(xor_r)
        a3, a4 = [], []
        for idx in range(0, len(na), 2):
            a3.append(na[idx])
            if idx + 1 < len(na):
                a4.append(na[idx + 1])
        merge = a3 + a4
        # encoding_conversion 参数顺序: a,b,c,e,d,t,f,r,n,o,i,_,x,u,s,l,v,h,p
        # y = [a, int(i), b, _, c, x, e, u, d, s, t, l, f, v, r, h, n, p, o]
        m = merge
        y = [m[0], int(m[10]), m[1], m[11], m[2], m[12], m[3], m[13],
             m[4], m[14], m[5], m[15], m[6], m[16], m[7], m[17],
             m[8], m[18] if len(m) > 18 else 0, m[9]]
        garbled = chr(2) + chr(255) + self._rc4_encrypt(
            "ÿ".encode("ISO-8859-1"), bytes(y[:19]).decode("ISO-8859-1").encode("ISO-8859-1")
        ).decode("ISO-8859-1")
        xb_ = ""
        idx = 0
        while idx < len(garbled):
            xb_ += self._calc(ord(garbled[idx]), ord(garbled[idx + 1]), ord(garbled[idx + 2]))
            idx += 3
        return f"{url_params}&X-Bogus={xb_}"


# ─── 直接 HTTP 抖音 API（替代 f2）───

_DY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)


def _gen_mstoken() -> str:
    """生成随机 msToken"""
    chars = string.ascii_letters + string.digits + "+/"
    return "".join(random.choices(chars, k=126)) + "=="


def _build_base_params(sec_uid: str, max_cursor: int = 0, count: int = 20) -> dict:
    """构建抖音 API 请求参数"""
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "sec_user_id": sec_uid,
        "max_cursor": str(max_cursor),
        "locate_query": "false",
        "show_live_replay_strategy": "1",
        "need_time_list": "1",
        "time_list_query": "0",
        "whale_cut_token": "",
        "cut_version": "1",
        "count": str(count),
        "publish_video_strategy_type": "2",
        "pc_client_type": "1",
        "version_code": "290100",
        "version_name": "29.1.0",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Edge",
        "browser_version": "130.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "130.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "12",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "100",
        "msToken": _gen_mstoken(),
    }


def _douyin_api_request(endpoint: str, params: dict, cookie: str) -> dict:
    """发送带签名的抖音 API 请求"""
    import httpx

    param_str = "&".join(f"{k}={v}" for k, v in params.items())
    xb = _XBogus(_DY_UA)
    signed_url = xb.get_xbogus(param_str)
    full_url = f"{endpoint}?{signed_url}"

    headers = {
        "User-Agent": _DY_UA,
        "Referer": "https://www.douyin.com/",
        "Cookie": cookie,
        "Accept": "application/json, text/plain, */*",
    }

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        resp = client.get(full_url, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _fetch_user_profile_direct(sec_uid: str, cookie: str) -> dict:
    """直接调用抖音 API 获取用户信息"""
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "sec_user_id": sec_uid,
        "pc_client_type": "1",
        "version_code": "290100",
        "version_name": "29.1.0",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Edge",
        "browser_version": "130.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "130.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "12",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "100",
        "msToken": _gen_mstoken(),
    }
    return _douyin_api_request("https://www.douyin.com/aweme/v1/web/user/profile/other/", params, cookie)


def _fetch_videos_direct(
    sec_uid: str, cookie: str, max_videos: int = 0, keyword: str = ""
) -> tuple[list[dict], str]:
    """直接调用抖音 API 获取视频列表（替代 f2）"""
    # 获取用户信息
    profile_data = _fetch_user_profile_direct(sec_uid, cookie)
    user = profile_data.get("user", {})
    creator_name = user.get("nickname", "")

    keywords = keyword.split() if keyword else None
    all_videos = []
    max_cursor = 0
    max_count = max_videos if max_videos > 0 else 99999

    while len(all_videos) < max_count:
        params = _build_base_params(sec_uid, max_cursor, count=20)
        data = _douyin_api_request(
            "https://www.douyin.com/aweme/v1/web/aweme/post/", params, cookie
        )

        aweme_list = data.get("aweme_list", [])
        if not aweme_list:
            break

        for item in aweme_list:
            vid = item.get("aweme_id", "")
            desc = item.get("desc", "无标题")
            title = re.sub(r"#\S+", "", desc).strip() or "无标题"
            duration = item.get("video", {}).get("duration", 0)
            if isinstance(duration, (int, float)) and duration > 10000:
                duration = duration // 1000
            music = item.get("music", {})
            play_url_list = (music.get("play_url") or {}).get("url_list", [])
            audio_url = play_url_list[0] if play_url_list else ""
            author = (item.get("author") or {}).get("nickname", creator_name)
            create_time = item.get("create_time", "")

            video = {
                "id": str(vid),
                "title": title,
                "raw_title": desc,
                "url": f"https://www.douyin.com/video/{vid}",
                "create_time": create_time,
                "duration": duration,
                "author": author,
                "audio_url": audio_url,
                "creator_name": creator_name,
            }

            if keywords:
                if _match_keyword(video, keywords):
                    all_videos.append(video)
            else:
                all_videos.append(video)

            if len(all_videos) >= max_count:
                break

        has_more = data.get("has_more", False)
        max_cursor = data.get("max_cursor", 0)
        if not has_more or not max_cursor:
            break

    return all_videos, creator_name


# ─── Apify 视频获取 ───


def _apify_fetch_videos(
    apify_token: str, profile_url: str, max_videos: int = 200
) -> list[dict]:
    """通过 Apify 获取视频列表"""
    from apify_client import ApifyClient

    client = ApifyClient(apify_token)

    actors = [
        {
            "id": "natanielsantos/douyin-scraper",
            "input": {
                "profileUrls": [profile_url],
                "searchTermsOrHashtags": [],
                "postUrls": [],
                "maxItemsPerUrl": max_videos,
                "profileSortFilter": "latest",
            },
        },
    ]

    for actor in actors:
        try:
            run = client.actor(actor["id"]).call(run_input=actor["input"])
            items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
            if items:
                return _normalize_video_list(items)
        except Exception:
            continue

    return []


def _normalize_video_list(items: list) -> list[dict]:
    """标准化视频列表"""
    videos = []
    seen_ids = set()

    for idx, item in enumerate(items):
        vid = str(item.get("id", item.get("aweme_id", f"_no_id_{idx}")))
        if vid in seen_ids:
            continue
        seen_ids.add(vid)

        raw_title = (
            item.get("text")
            or item.get("desc")
            or item.get("title")
            or item.get("description")
            or "无标题"
        )
        title = re.sub(r"#\S+", "", raw_title).strip() or "无标题"

        author_meta = item.get("authorMeta") or {}
        author = author_meta.get("name") or item.get("author") or ""

        stats = item.get("statistics") or {}
        digg_count = stats.get("diggCount") or item.get("digg_count") or 0

        video_meta = item.get("videoMeta") or {}
        duration = video_meta.get("duration") or item.get("duration") or 0
        if duration > 10000:
            duration = duration // 1000

        music_meta = item.get("musicMeta") or {}
        audio_url = music_meta.get("playUrl") or ""

        videos.append({
            "id": vid,
            "title": title,
            "raw_title": raw_title,
            "url": item.get("url", ""),
            "create_time": item.get("createTime", item.get("create_time", "")),
            "duration": duration,
            "digg_count": digg_count,
            "author": author,
            "audio_url": audio_url,
        })

    videos.sort(key=lambda x: str(x.get("create_time", "")), reverse=True)
    return videos


# ─── Whisper 转录 ───


def _call_whisper(audio_path: str, api_key: str, proxy: str = "") -> dict:
    """调用 OpenAI Whisper API"""
    import httpx
    from openai import OpenAI

    client_kwargs = {"api_key": api_key}
    if proxy:
        client_kwargs["http_client"] = httpx.Client(
            proxy=proxy, timeout=httpx.Timeout(300, connect=60)
        )
    else:
        client_kwargs["http_client"] = httpx.Client(
            timeout=httpx.Timeout(300, connect=60)
        )

    client = OpenAI(**client_kwargs)

    with open(audio_path, "rb") as f:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="zh",
            response_format="verbose_json",
            timestamp_granularities=["segment"],
            prompt="以下是普通话的句子，包含标点符号。",
        )

    segments = []
    for seg in getattr(response, "segments", []) or []:
        start = seg.start if hasattr(seg, "start") else seg.get("start", 0)
        end = seg.end if hasattr(seg, "end") else seg.get("end", 0)
        text = seg.text if hasattr(seg, "text") else seg.get("text", "")
        segments.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "text": text.strip(),
        })

    return {
        "text": response.text.strip(),
        "segments": segments,
        "language": getattr(response, "language", "zh"),
    }


# ─── Word 文档生成 ───


def _generate_word_doc(videos: list[dict], creator_name: str) -> bytes:
    """生成 Word 文档，返回字节"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor

    doc = Document()

    # 默认样式
    style = doc.styles["Normal"]
    style.font.size = Pt(12)
    style.font.name = "Arial"
    style.paragraph_format.line_spacing = 1.5

    for section in doc.sections:
        section.top_margin = Cm(2.54)
        section.bottom_margin = Cm(2.54)
        section.left_margin = Cm(3.18)
        section.right_margin = Cm(3.18)

    # 封面
    doc.add_paragraph()
    doc.add_paragraph()
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(creator_name)
    run.font.size = Pt(36)
    run.bold = True
    run.font.color.rgb = RGBColor(0x1A, 0x1A, 0x2E)

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = subtitle.add_run("抖音视频文字稿合集")
    run.font.size = Pt(20)
    run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

    doc.add_paragraph()
    total = len(videos)
    transcribed = sum(1 for v in videos if v.get("transcript"))
    info = doc.add_paragraph()
    info.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = info.add_run(f"共 {total} 个视频 · 已转录 {transcribed} 个")
    run.font.size = Pt(12)
    run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

    date_p = doc.add_paragraph()
    date_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = date_p.add_run(f"生成日期：{datetime.now().strftime('%Y年%m月%d日')}")
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor(0xAA, 0xAA, 0xAA)

    doc.add_page_break()

    # 目录
    doc.add_heading("目 录", level=1)
    chapter_num = 0
    for v in videos:
        if v.get("transcript"):
            chapter_num += 1
            t = re.sub(r"#\S+", "", v.get("title", f"视频 {chapter_num}")).strip()[:60]
            p = doc.add_paragraph(f"{chapter_num}. {t}")
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)

    doc.add_page_break()

    # 正文
    chapter_num = 0
    for v in videos:
        transcript = v.get("transcript")
        if not transcript:
            continue
        chapter_num += 1
        t = re.sub(r"#\S+", "", v.get("title", f"视频 {chapter_num}")).strip()[:80]

        doc.add_heading(f"{chapter_num}. {t}", level=2)

        # 元信息
        meta_parts = []
        ct = v.get("create_time", "")
        if ct:
            if isinstance(ct, (int, float)):
                try:
                    ct = datetime.fromtimestamp(ct).strftime("%Y-%m-%d")
                except Exception:
                    ct = ""
            if ct:
                meta_parts.append(f"发布时间：{str(ct)[:10]}")
        dur = v.get("duration", 0)
        if isinstance(dur, (int, float)) and dur > 0:
            m, s = divmod(int(dur), 60)
            meta_parts.append(f"时长：{m}:{s:02d}")
        if meta_parts:
            mp = doc.add_paragraph(" | ".join(meta_parts))
            for r in mp.runs:
                r.font.size = Pt(9)
                r.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

        # 文字稿
        text = ""
        segments = []
        if isinstance(transcript, dict):
            text = transcript.get("text", "")
            segments = transcript.get("segments", [])
        elif isinstance(transcript, str):
            text = transcript

        if segments and len(segments) > 1:
            for seg in segments:
                sp = doc.add_paragraph()
                sp.paragraph_format.line_spacing = 1.8
                start_sec = seg.get("start", 0)
                ts = _format_ts(start_sec)
                ts_run = sp.add_run(f"[{ts}] ")
                ts_run.font.size = Pt(9)
                ts_run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
                seg_text = seg.get("text", "").strip()
                if seg_text:
                    text_run = sp.add_run(seg_text)
                    text_run.font.size = Pt(12)
        elif text:
            cp = doc.add_paragraph(text)
            cp.paragraph_format.line_spacing = 1.8
            cp.paragraph_format.first_line_indent = Pt(24)

        # 分隔线
        sep = doc.add_paragraph()
        sep.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = sep.add_run("─" * 30)
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0xDD, 0xDD, 0xDD)

    # 输出字节
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _format_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ─── GPT 对话 ───


def _chat_with_creator(
    message: str,
    creator_name: str,
    videos_context: list[dict],
    history: list[dict],
    api_key: str,
    proxy: str = "",
) -> str:
    """与博主 GPT 对话"""
    import httpx
    from openai import OpenAI

    client_kwargs = {"api_key": api_key}
    if proxy:
        client_kwargs["http_client"] = httpx.Client(proxy=proxy, timeout=120)
    else:
        client_kwargs["http_client"] = httpx.Client(timeout=120)

    client = OpenAI(**client_kwargs)

    # 构建博主资料
    profile_parts = []
    for v in videos_context[:50]:
        t = v.get("transcript")
        if t:
            text = t.get("text", "") if isinstance(t, dict) else str(t)
            if text:
                title = v.get("title", "")
                profile_parts.append(f"【{title}】\n{text[:2000]}")

    profile = "\n\n---\n\n".join(profile_parts) if profile_parts else "暂无内容"

    # 搜索相关内容
    query_words = set()
    phrases = re.split(r"[，。？！、；：\s,.\?!;:\n]+", message)
    for p in phrases:
        p = p.strip()
        if 2 <= len(p) <= 6:
            query_words.add(p)
        for n in (2, 3):
            for i in range(len(p) - n + 1):
                query_words.add(p[i : i + n])

    context_parts = []
    if query_words:
        scored = []
        for v in videos_context:
            t = v.get("transcript")
            if not t:
                continue
            text = t.get("text", "") if isinstance(t, dict) else str(t)
            score = sum(1 for w in query_words if w in text)
            if score > 0:
                scored.append((score, v.get("title", ""), text[:2000]))
        scored.sort(key=lambda x: x[0], reverse=True)
        for _, title, text in scored[:5]:
            context_parts.append(f"【{title}】\n{text}")

    context = "\n\n---\n\n".join(context_parts) if context_parts else "（无特别相关内容）"

    system_prompt = f"""你是抖音博主「{creator_name}」的 AI 分身。模仿这位博主的思维方式、说话风格来回答。

## 博主视频内容样本
{profile[:100000]}

## 当前对话相关参考
{context[:30000]}

## 规则
1. 用第一人称，保持博主风格
2. 优先用博主表达过的观点
3. 不说"根据视频"这样的元叙述
4. 自然口语化"""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-20:])
    messages.append({"role": "user", "content": message})

    response = client.chat.completions.create(
        model="gpt-4.1",
        max_tokens=2048,
        messages=messages,
    )

    if not response.choices:
        return "对话出错: 空响应"

    return response.choices[0].message.content
