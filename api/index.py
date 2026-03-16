"""Vercel Serverless API — 抖音视频文字稿提取

Flask 应用，提供以下 API：
- POST /api/resolve-url    解析抖音链接 → sec_uid
- POST /api/fetch-videos   获取博主视频列表
- POST /api/transcribe     转录单个视频音频
- POST /api/generate-doc   生成 Word 文档
- POST /api/chat           博主 GPT 对话
"""

import asyncio
import json
import os
import re
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

    # 优先 f2（免费、无限制）
    if not use_apify and cookie:
        try:
            videos, creator_name = asyncio.run(
                _f2_fetch_videos(sec_uid, cookie, max_videos, keyword)
            )
            return jsonify({
                "videos": videos,
                "creator_name": creator_name,
                "total": len(videos),
                "method": "f2",
            })
        except Exception as e:
            if not apify_token:
                return jsonify({"error": f"f2 获取失败: {e}"}), 500

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


# ─── f2 视频获取 ───


async def _f2_fetch_videos(
    sec_uid: str, cookie: str, max_videos: int = 0, keyword: str = ""
) -> tuple[list[dict], str]:
    """使用 f2 框架获取视频列表"""
    import logging

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("f2").setLevel(logging.WARNING)
    os.environ.setdefault("F2_BARK_KEY", "")

    from f2.apps.douyin.handler import DouyinHandler

    kwargs = {
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_9) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/142.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.douyin.com/",
        },
        "cookie": cookie,
        "proxies": {"http://": None, "https://": None},
    }
    handler = DouyinHandler(kwargs)

    profile = await handler.fetch_user_profile(sec_user_id=sec_uid)
    creator_name = profile.nickname or ""

    keywords = keyword.split() if keyword else None
    all_videos = []

    async for page_filter in handler.fetch_user_post_videos(
        sec_user_id=sec_uid,
        max_counts=max_videos if max_videos > 0 else None,
    ):
        aweme_ids = page_filter.aweme_id
        if not aweme_ids:
            break
        if not isinstance(aweme_ids, list):
            aweme_ids = [aweme_ids]

        descs = page_filter.desc
        if not isinstance(descs, list):
            descs = [descs]
        nicknames = page_filter.nickname
        if not isinstance(nicknames, list):
            nicknames = [nicknames]
        create_times = page_filter.create_time
        if not isinstance(create_times, list):
            create_times = [create_times]
        durations = page_filter.video_duration
        if not isinstance(durations, list):
            durations = [durations]
        music_urls = page_filter.music_play_url
        if not isinstance(music_urls, list):
            music_urls = [music_urls]

        for i, vid in enumerate(aweme_ids):

            def _safe(lst, idx, default=""):
                try:
                    return lst[idx] if isinstance(lst, list) and idx < len(lst) else default
                except (IndexError, TypeError):
                    return default

            desc = _safe(descs, i, "无标题")
            title = re.sub(r"#\S+", "", desc).strip() or "无标题"
            duration = _safe(durations, i, 0)
            if isinstance(duration, (int, float)) and duration > 10000:
                duration = duration // 1000

            video = {
                "id": str(vid),
                "title": title,
                "raw_title": desc,
                "url": f"https://www.douyin.com/video/{vid}",
                "create_time": _safe(create_times, i, ""),
                "duration": duration,
                "author": _safe(nicknames, i, creator_name),
                "audio_url": _safe(music_urls, i, ""),
                "creator_name": creator_name,
            }

            if keywords:
                if _match_keyword(video, keywords):
                    all_videos.append(video)
            else:
                all_videos.append(video)

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
