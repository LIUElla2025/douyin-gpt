"""抖音博主视频数据获取模块

方案优先级:
1. f2 框架（免费、无数量限制、可获取全部视频）
2. Apify 抖音专用 Actor（备选，有 50 个限制）
3. yt-dlp DouyinIE（最后备选）
"""

import json
import subprocess
import re
import threading
import asyncio
import urllib.request
import urllib.parse
from pathlib import Path
from config import APIFY_API_TOKEN, TEMP_DIR, AUDIO_DIR, TRANSCRIPTS_DIR, sanitize_id


def resolve_douyin_input(user_input: str) -> str:
    """将用户输入（短链接、完整URL、抖音号）解析为可用的主页 URL。

    返回格式: https://www.douyin.com/user/{sec_uid}
    如果无法解析，返回原始输入构造的 URL。
    """
    user_input = user_input.strip()

    # 如果是短链接 (v.douyin.com)
    if "v.douyin.com" in user_input or "douyin.com/share" in user_input:
        # 提取 URL
        url_match = re.search(r'https?://[^\s]+', user_input)
        if url_match:
            short_url = url_match.group(0)
            try:
                req = urllib.request.Request(short_url, method='HEAD')
                req.add_header('User-Agent', 'Mozilla/5.0')
                resp = urllib.request.urlopen(req, timeout=10)
                final_url = resp.url
                # 从重定向 URL 中提取 sec_uid
                sec_uid_match = re.search(r'sec_uid=([^&]+)', final_url)
                if sec_uid_match:
                    sec_uid = urllib.parse.unquote(sec_uid_match.group(1))
                    return f"https://www.douyin.com/user/{sec_uid}"
                # 或者从 /user/ 路径中提取
                user_match = re.search(r'/user/([^?&]+)', final_url)
                if user_match:
                    return f"https://www.douyin.com/user/{user_match.group(1)}"
            except Exception as e:
                print(f"  短链接解析失败: {e}")

    # 如果已经是完整的 douyin.com/user/ URL
    if "douyin.com/user/" in user_input:
        user_match = re.search(r'douyin\.com/user/([^?&\s]+)', user_input)
        if user_match:
            return f"https://www.douyin.com/user/{user_match.group(1)}"

    # 如果是纯 sec_uid (MS4wLjABAAAA 开头)
    if user_input.startswith("MS4wLjABAAAA"):
        return f"https://www.douyin.com/user/{user_input}"

    # 普通抖音号/UID — 尝试通过访问抖音主页获取 sec_uid
    # natanielsantos/douyin-scraper 需要 sec_uid URL 才能正常抓取
    try:
        probe_url = f"https://www.douyin.com/user/{user_input}"
        req = urllib.request.Request(probe_url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36')
        resp = urllib.request.urlopen(req, timeout=10)
        final_url = resp.url
        # 从重定向后的 URL 中提取 sec_uid
        sec_uid_match = re.search(r'sec_uid=([^&]+)', final_url)
        if sec_uid_match:
            sec_uid = urllib.parse.unquote(sec_uid_match.group(1))
            print(f"  数字 ID {user_input} → sec_uid: {sec_uid[:30]}...")
            return f"https://www.douyin.com/user/{sec_uid}"
        user_match = re.search(r'/user/([^?&\s]+)', final_url)
        if user_match and user_match.group(1) != user_input:
            return f"https://www.douyin.com/user/{user_match.group(1)}"
    except Exception as e:
        print(f"  数字 ID 转 sec_uid 失败: {e}")

    return f"https://www.douyin.com/user/{user_input}"


# ─── 方案0: f2 框架（免费、无限制）───


def _get_douyin_cookie() -> str:
    """从环境变量获取抖音 Cookie"""
    import os
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
    return os.getenv("DOUYIN_COOKIE", "")


def _extract_sec_uid(profile_url: str) -> str:
    """从主页 URL 提取 sec_uid"""
    match = re.search(r'/user/([^?&\s]+)', profile_url)
    return match.group(1) if match else ""


def _get_checkpoint_path(douyin_id: str) -> str:
    """获取视频列表断点文件路径"""
    safe_id = sanitize_id(douyin_id)
    return str(TEMP_DIR / f"{safe_id}_videos.checkpoint.jsonl")


def load_checkpoint_videos(douyin_id: str) -> list[dict]:
    """加载断点文件中已获取的视频列表"""
    cp_path = _get_checkpoint_path(douyin_id)
    if not Path(cp_path).exists():
        return []
    videos = []
    seen = set()
    try:
        with open(cp_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                v = json.loads(line)
                vid = v.get("id", "")
                if vid and vid not in seen:
                    videos.append(v)
                    seen.add(vid)
    except Exception:
        return []
    return videos


def clear_checkpoint(douyin_id: str):
    """清理断点文件（全部完成后调用）"""
    cp_path = Path(_get_checkpoint_path(douyin_id))
    cp_path.unlink(missing_ok=True)


def clear_all_data(douyin_id: str):
    """彻底清空该博主的所有缓存数据（重新下载时调用）"""
    from config import DATA_DIR
    safe_id = sanitize_id(douyin_id)
    # 清除 checkpoint + cursor
    clear_checkpoint(douyin_id)
    cursor_path = Path(_get_checkpoint_path(douyin_id).replace(".jsonl", ".cursor.json"))
    cursor_path.unlink(missing_ok=True)
    # 清除视频列表 JSON
    videos_path = TRANSCRIPTS_DIR / f"{safe_id}_videos.json"
    videos_path.unlink(missing_ok=True)
    # 清除转录结果 JSON
    transcripts_path = TRANSCRIPTS_DIR / f"{safe_id}_transcripts.json"
    transcripts_path.unlink(missing_ok=True)
    # 清除 Apify 原始数据（如有）
    raw_path = TRANSCRIPTS_DIR / f"{safe_id}_raw_apify.json"
    raw_path.unlink(missing_ok=True)
    # 清除音频文件目录（防止旧文件被复用）
    for audio_file in AUDIO_DIR.glob("*.mp3"):
        audio_file.unlink(missing_ok=True)
    # 清除临时视频文件
    for tmp_file in TEMP_DIR.glob("*"):
        if tmp_file.name != ".DS_Store":
            tmp_file.unlink(missing_ok=True)
    # 清除对话历史
    history_dir = DATA_DIR / "chat_history"
    if history_dir.exists():
        for f in history_dir.glob("*.json"):
            f.unlink(missing_ok=True)
    # 清除输出的 Word 文档
    from config import OUTPUT_DIR
    for doc_file in OUTPUT_DIR.glob("*.docx"):
        doc_file.unlink(missing_ok=True)
    print(f"  已清空「{douyin_id}」的所有缓存数据")


def f2_get_creator_videos(douyin_id: str, max_videos: int = None, profile_url: str = None,
                          progress_callback=None, keyword: str = "") -> list[dict]:
    """通过 f2 框架获取博主视频列表 — 支持关键词边获取边过滤 + 断点续传"""
    cookie = _get_douyin_cookie()
    if not cookie:
        print("  未设置 DOUYIN_COOKIE，跳过 f2 方案")
        return []

    sec_uid = _extract_sec_uid(profile_url) if profile_url else douyin_id
    checkpoint_path = _get_checkpoint_path(douyin_id)

    # 用 subprocess 运行独立的 f2_worker.py，完全隔离 Streamlit 环境
    import sys
    worker_script = str(Path(__file__).resolve().parent / "f2_worker.py")
    python_exe = sys.executable

    cmd = [python_exe, "-u", worker_script, sec_uid]  # -u 禁用缓冲
    if max_videos:
        cmd.append(str(max_videos))
    if keyword:
        cmd.extend(["--keyword", keyword])
    cmd.extend(["--checkpoint", checkpoint_path])

    # 通过环境变量传递 cookie（避免命令行参数长度/转义问题）
    import os
    env = os.environ.copy()
    env["DOUYIN_COOKIE"] = cookie

    print(f"  f2: 启动子进程获取视频...")
    try:
        import time as _time

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(Path(__file__).resolve().parent),
            env=env,
        )

        # 后台线程读 stderr 进度，存到共享变量
        import threading
        stderr_lines = []
        latest_progress = [0.0, "f2 启动中..."]  # [progress, text]
        recent_titles = []  # 最近的视频标题或匹配标题
        last_update_time = [_time.time()]  # 上次收到 stderr 更新的时间

        def _read_stderr():
            for line in proc.stderr:
                line = line.strip()
                if not line:
                    continue
                stderr_lines.append(line)
                # 解析不同类型的进度消息
                if line.startswith("f2_progress: "):
                    msg = line[len("f2_progress: "):]
                    # 解析 "已扫描 200/10483 | 匹配 5 个" 或 "已获取 200/10483 个视频"
                    m = re.search(r'(\d+)/(\d+)', msg)
                    if m:
                        got, total = int(m.group(1)), int(m.group(2))
                        latest_progress[0] = min(got / max(total, 1), 0.95)
                    latest_progress[1] = msg
                elif line.startswith("f2_title: "):
                    title = line[len("f2_title: "):]
                    recent_titles.append(title)
                elif line.startswith("f2_info: "):
                    latest_progress[1] = line[len("f2_info: "):]
                last_update_time[0] = _time.time()
                if line.startswith("f2_done: "):
                    latest_progress[0] = 1.0
                    latest_progress[1] = line[len("f2_done: "):]

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        # 主线程轮询：检查进程是否结束 + 回调进度给 UI
        start_time = _time.time()
        stdout_chunks = []

        while True:
            # 无超时限制 — 视频多的博主可能需要很长时间

            # 回调进度给 Streamlit（主线程，UI 能刷新）
            if progress_callback:
                elapsed = int(_time.time() - start_time)
                idle_sec = int(_time.time() - last_update_time[0])
                elapsed_str = f"{elapsed // 60}分{elapsed % 60:02d}秒" if elapsed >= 60 else f"{elapsed}秒"

                # 进度文本附带最近获取的视频标题
                progress_text = latest_progress[1]

                # 长时间无新数据时显示耗时和活动提示
                if idle_sec > 5:
                    dots = "." * (1 + (elapsed % 3))
                    progress_text += f" | 已耗时 {elapsed_str}，正在处理{dots}"
                else:
                    progress_text += f" | {elapsed_str}"

                # 标题列表单独用 \n--- 分隔传递，避免混入 progress_bar text
                if recent_titles:
                    progress_text += "\n---\n" + "\n".join(recent_titles)
                progress_callback(latest_progress[0], progress_text)

            # 检查进程是否结束
            retcode = proc.poll()
            if retcode is not None:
                # 进程结束，读取剩余 stdout
                remaining = proc.stdout.read()
                if remaining:
                    stdout_chunks.append(remaining)
                break

            # 非阻塞读一小段 stdout（避免 pipe 缓冲区满导致死锁）
            import select
            readable, _, _ = select.select([proc.stdout], [], [], 0.5)
            if readable:
                chunk = proc.stdout.read(65536)
                if chunk:
                    stdout_chunks.append(chunk)

        stderr_thread.join(timeout=5)
        stdout = "".join(stdout_chunks)

        if retcode != 0:
            print(f"  f2 子进程失败 (exit {retcode})")
            if stderr_lines:
                print(f"  最后输出: {stderr_lines[-1]}")
            return []

        if not stdout or not stdout.strip():
            print("  f2 子进程返回空数据")
            return []

        videos = json.loads(stdout)
        print(f"  f2: 共获取 {len(videos)} 个视频")
        return videos

    except json.JSONDecodeError as e:
        print(f"  f2 子进程返回无效 JSON: {e}")
        return []
    except Exception as e:
        print(f"  f2 子进程异常: {type(e).__name__}: {e}")
        return []


# ─── 方案1: Apify 抖音 Actor ───


def apify_get_creator_videos(douyin_id: str, max_videos: int = 200, profile_url: str = None) -> list[dict]:
    """通过 Apify 抖音专用 Actor 获取博主视频列表"""
    if not APIFY_API_TOKEN:
        raise ValueError("请设置 APIFY_API_TOKEN 环境变量")

    from apify_client import ApifyClient
    client = ApifyClient(APIFY_API_TOKEN)

    if not profile_url:
        profile_url = f"https://www.douyin.com/user/{douyin_id}"

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
        {
            "id": "apibox/douyin-user-post-scraper",
            "input": {"userId": douyin_id, "maxItems": max_videos},
        },
        {
            "id": "easyapi/douyin-video-downloader",
            "input": {"url": profile_url, "maxItems": max_videos},
        },
    ]

    for actor in actors:
        try:
            run = client.actor(actor["id"]).call(run_input=actor["input"])
            items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
            if items:
                # 保存原始数据用于调试
                raw_path = TRANSCRIPTS_DIR / f"{sanitize_id(douyin_id)}_raw_apify.json"
                with open(raw_path, "w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False, indent=2, default=str)
                print(f"  原始 Apify 数据已保存到: {raw_path}")
                return _normalize_video_list(items)
        except Exception as e:
            print(f"  Apify Actor {actor['id']} 失败: {e}")
            continue

    return []


def apify_get_transcripts(douyin_id: str, video_urls: list[str] = None) -> list[dict]:
    """通过 Apify 高精度抖音文字稿 Actor 直接获取字幕"""
    if not APIFY_API_TOKEN:
        return []

    from apify_client import ApifyClient
    client = ApifyClient(APIFY_API_TOKEN)

    try:
        run_input = {"userId": douyin_id}
        if video_urls:
            run_input["urls"] = video_urls

        run = client.actor("apple_yang/high-accuracy-douyin-transcripts-scraper").call(
            run_input=run_input
        )
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
        return items
    except Exception as e:
        print(f"  Apify 文字稿 Actor 失败: {e}")
        return []


# ─── 方案2: douyin-tiktok-scraper (免费 fallback) ───


def pypi_get_creator_videos(douyin_id: str, max_videos: int = 200) -> list[dict]:
    """使用 douyin-tiktok-scraper PyPI 包获取博主视频"""
    try:
        import asyncio
        from douyin_tiktok_scraper.scraper import Scraper

        scraper = Scraper()

        async def _fetch():
            url = f"https://www.douyin.com/user/{douyin_id}"
            data = await scraper.hybrid_parsing(url)
            return data

        # 在独立线程中运行 asyncio，避免和 Streamlit 事件循环冲突
        result_holder = [None]
        error_holder = [None]

        def _run_in_thread():
            try:
                result_holder[0] = asyncio.run(_fetch())
            except Exception as e:
                error_holder[0] = e

        t = threading.Thread(target=_run_in_thread, daemon=True)
        t.start()
        t.join(timeout=60)

        if t.is_alive():
            print("  douyin-tiktok-scraper 超时（60秒）")
            return []

        if error_holder[0]:
            raise error_holder[0]

        result = result_holder[0]
        if isinstance(result, dict) and result.get("video_data"):
            return _normalize_video_list(result["video_data"][:max_videos])
        if isinstance(result, list):
            return _normalize_video_list(result[:max_videos])

    except ImportError:
        print("  douyin-tiktok-scraper 未安装，跳过此方案")
    except Exception as e:
        print(f"  douyin-tiktok-scraper 失败: {e}")

    return []


# ─── 方案3: yt-dlp (最后备选) ───


def ytdlp_get_creator_videos(douyin_url: str) -> list[dict]:
    """使用 yt-dlp 获取抖音博主视频列表（不太稳定）"""
    try:
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--dump-json",
            "--no-download",
            douyin_url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        if result.returncode != 0:
            return []

        videos = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                try:
                    data = json.loads(line)
                    videos.append(data)
                except json.JSONDecodeError:
                    continue
        return _normalize_video_list(videos)
    except FileNotFoundError:
        print("  yt-dlp 未安装，跳过此方案")
    except subprocess.TimeoutExpired:
        print("  yt-dlp 超时")
    except Exception as e:
        print(f"  yt-dlp 失败: {e}")
    return []


# ─── 统一入口 ───


def get_creator_videos(douyin_id: str, max_videos: int = 200, progress_callback=None,
                       keyword: str = "") -> list[dict]:
    """获取博主视频列表 — 支持关键词边获取边过滤"""

    # 解析用户输入（支持短链接、完整URL、抖音号）
    if progress_callback:
        progress_callback(0.02, "解析抖音链接...")
    profile_url = resolve_douyin_input(douyin_id)
    print(f"  解析后的主页 URL: {profile_url}")

    errors = []

    # f2 框架获取视频（有关键词时边获取边过滤）
    if progress_callback:
        if keyword:
            progress_callback(0.05, f"f2 搜索关键词「{keyword}」...")
        else:
            progress_callback(0.05, "f2 获取博主视频...")
    try:
        videos = f2_get_creator_videos(
            douyin_id, max_videos, profile_url=profile_url,
            progress_callback=progress_callback, keyword=keyword,
        )
        if videos:
            if progress_callback:
                progress_callback(1.0, f"通过 f2 获取到 {len(videos)} 个视频")
            return videos
        else:
            errors.append("f2: 返回空列表")
    except Exception as e:
        errors.append(f"f2: {e}")
        print(f"  f2 失败: {e}")

    error_detail = "\n".join(f"  - {e}" for e in errors) if errors else ""
    raise RuntimeError(
        f"所有方案均无法获取抖音号 '{douyin_id}' 的视频。\n"
        "请检查：\n"
        "1. 抖音号是否正确（注意不是昵称，是抖音号）\n"
        "2. APIFY_API_TOKEN 是否已设置\n"
        "3. 是否已安装 douyin-tiktok-scraper (pip install douyin-tiktok-scraper)\n"
        f"\n错误详情:\n{error_detail}"
    )


def _fetch_fresh_video_url(aweme_id: str) -> str:
    """调用 f2 详情 API 获取视频的实时播放 URL（URL 有效期只有几分钟）"""
    import sys as _sys
    cookie = _get_douyin_cookie()
    if not cookie or not aweme_id:
        return ""

    worker_script = str(Path(__file__).resolve().parent / "f2_detail_worker.py")
    python_exe = _sys.executable

    import os as _os
    env = _os.environ.copy()
    env["DOUYIN_COOKIE"] = cookie

    try:
        result = subprocess.run(
            [python_exe, "-u", worker_script, "--single", aweme_id],
            capture_output=True, text=True, timeout=60,
            cwd=str(Path(__file__).resolve().parent),
            env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            url = data.get("video_play_url", "")
            if isinstance(url, list):
                url = url[0] if url else ""
            return url
    except Exception as e:
        print(f"  获取实时链接失败: {e}")
    return ""


def download_video_audio(video: dict, index: int) -> Path | None:
    """下载视频并提取口述音频

    核心逻辑：用视频 ID 实时获取新鲜的 video_play_url（避免过期链接），
    下载视频后用 ffmpeg 提取音频。
    不使用 audio_url（背景音乐）作为 fallback，避免所有视频转录相同。
    """
    safe_title = re.sub(r'[^\w\u4e00-\u9fff]', '_', video.get("title", f"video_{index}"))[:50]
    output_path = AUDIO_DIR / f"{index:04d}_{safe_title}.mp3"

    if output_path.exists():
        return output_path

    aweme_id = video.get("id", "")

    # ─── 方案1: 实时获取视频直链 → 下载视频 → ffmpeg 提取音频 ───
    print(f"  [{index}] 获取实时链接: {video.get('title', '')[:30]}...")
    fresh_url = _fetch_fresh_video_url(aweme_id)

    if fresh_url:
        temp_video = TEMP_DIR / f"{index:04d}_video.mp4"
        try:
            req = urllib.request.Request(fresh_url)
            req.add_header('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_9) AppleWebKit/537.36')
            req.add_header('Referer', 'https://www.douyin.com/')
            resp = urllib.request.urlopen(req, timeout=60)
            with open(temp_video, 'wb') as f:
                f.write(resp.read())

            if temp_video.stat().st_size > 10000:  # 至少 10KB（视频文件应该更大）
                ffmpeg_cmd = [
                    "ffmpeg", "-y", "-i", str(temp_video),
                    "-vn", "-acodec", "libmp3lame", "-q:a", "5",
                    str(output_path),
                ]
                ffmpeg_result = subprocess.run(
                    ffmpeg_cmd, capture_output=True, text=True, timeout=120,
                )
                temp_video.unlink(missing_ok=True)

                if ffmpeg_result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1000:
                    return output_path
                else:
                    print(f"  [{index}] ffmpeg 提取音频失败")
                    output_path.unlink(missing_ok=True)
            else:
                size = temp_video.stat().st_size if temp_video.exists() else 0
                print(f"  [{index}] 视频文件太小 ({size} bytes)，可能链接已过期")
                temp_video.unlink(missing_ok=True)
        except Exception as e:
            print(f"  [{index}] 视频下载失败: {e}")
            temp_video.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)
    else:
        print(f"  [{index}] 无法获取实时视频链接")

    # ─── 不使用 audio_url (背景音乐) 作为 fallback ───
    # 背景音乐转录会导致所有视频文稿完全相同
    return None


def download_all_audios(videos: list[dict], progress_callback=None) -> list[dict]:
    """批量下载所有视频的音频"""
    results = []
    total = len(videos)

    for i, video in enumerate(videos):
        if progress_callback:
            progress_callback(
                i / max(total, 1),
                f"下载音频 ({i+1}/{total}): {video.get('title', '')[:30]}",
            )

        audio_path = download_video_audio(video, i)
        results.append({
            **video,
            "audio_path": str(audio_path) if audio_path else None,
            "downloaded": audio_path is not None,
        })

    return results


# ─── 工具函数 ───


def _normalize_video_list(items: list) -> list[dict]:
    """将不同来源的视频数据标准化

    natanielsantos/douyin-scraper 返回:
      id, text, createTime, url, authorMeta.name, statistics.diggCount, videoMeta.duration
    其他来源可能用: desc, video_url, digg_count 等
    """
    videos = []
    seen_ids = set()

    for idx, item in enumerate(items):
        vid = str(item.get("id", item.get("aweme_id", item.get("video_id", ""))))

        if not vid:
            vid = f"_no_id_{idx}"

        if vid in seen_ids:
            continue
        seen_ids.add(vid)

        # 描述文案: natanielsantos 用 text, 其他可能用 desc/title/description
        raw_title = (
            item.get("text")
            or item.get("desc")
            or item.get("title")
            or item.get("description")
            or "无标题"
        )
        # 保留原始标题（含 #标签）用于搜索，清理后的用于显示
        title = re.sub(r'#\S+', '', raw_title).strip()

        # 作者: natanielsantos 用嵌套 authorMeta.name
        author_meta = item.get("authorMeta") or {}
        author = (
            author_meta.get("name")
            or item.get("author")
            or item.get("nickname")
            or item.get("author_name")
            or ""
        )

        # 统计: natanielsantos 用嵌套 statistics.diggCount
        stats = item.get("statistics") or {}
        digg_count = (
            stats.get("diggCount")
            or item.get("digg_count")
            or item.get("diggCount")
            or item.get("like_count")
            or 0
        )

        # 时长: natanielsantos 用 videoMeta.duration (毫秒)
        video_meta = item.get("videoMeta") or {}
        duration = video_meta.get("duration") or item.get("duration") or 0
        if duration > 10000:  # 毫秒转秒
            duration = duration // 1000

        # 音频直链: natanielsantos 返回 musicMeta.playUrl (直接 MP3)
        music_meta = item.get("musicMeta") or {}
        audio_url = music_meta.get("playUrl") or ""

        # 视频播放链接: videoMeta.playUrl
        video_play_url = video_meta.get("playUrl") or ""

        video = {
            "id": vid,
            "title": title,
            "raw_title": raw_title,
            "url": item.get("url", item.get("video_url", item.get("videoUrl", ""))),
            "share_url": item.get("share_url", item.get("shareUrl", item.get("webpage_url", ""))),
            "create_time": item.get("createTime", item.get("create_time", item.get("timestamp", ""))),
            "duration": duration,
            "digg_count": digg_count,
            "author": author,
            "audio_url": audio_url,
            "video_play_url": video_play_url,
        }
        videos.append(video)

    videos.sort(key=lambda x: str(x.get("create_time", "")), reverse=True)
    return videos


def save_video_list(videos: list[dict], douyin_id: str) -> Path:
    """保存视频列表到 JSON"""
    safe_id = sanitize_id(douyin_id)
    output_path = TRANSCRIPTS_DIR / f"{safe_id}_videos.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(videos, f, ensure_ascii=False, indent=2)
    return output_path


def fill_missing_audio_urls(videos: list[dict], progress_callback=None) -> int:
    """用 f2 的详情 API 为缺少 video_play_url 的视频补全下载链接

    优先补全 video_play_url（视频直链，含口述音频），
    同时也补全 audio_url 作为备选。

    Returns:
        成功补全的数量
    """
    missing = [(i, v) for i, v in enumerate(videos)
               if not v.get("video_play_url") and v.get("id")]
    if not missing:
        return 0

    cookie = _get_douyin_cookie()
    if not cookie:
        print("  未设置 DOUYIN_COOKIE，无法补全音频链接")
        return 0

    import sys

    # 在子进程中运行 f2 详情 API（避免 Streamlit 事件循环冲突）
    worker_script = str(Path(__file__).resolve().parent / "f2_detail_worker.py")
    python_exe = sys.executable

    video_ids = [v.get("id") for _, v in missing]
    cmd = [python_exe, "-u", worker_script]

    import os
    env = os.environ.copy()
    env["DOUYIN_COOKIE"] = cookie

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(Path(__file__).resolve().parent),
            env=env,
        )
        # 通过 stdin 传入视频ID列表
        input_data = json.dumps(video_ids)
        stdout, stderr = proc.communicate(input=input_data, timeout=600)

        if stderr:
            # 打印进度信息
            for line in stderr.strip().split("\n"):
                if line.strip():
                    print(f"  {line}")
                    if progress_callback and line.startswith("detail_progress:"):
                        msg = line[len("detail_progress:"):].strip()
                        import re as _re
                        m = _re.search(r'(\d+)/(\d+)', msg)
                        if m:
                            done, total = int(m.group(1)), int(m.group(2))
                            progress_callback(done / max(total, 1), msg)

        if proc.returncode != 0:
            print(f"  f2 详情获取失败 (exit {proc.returncode})")
            return 0

        if not stdout or not stdout.strip():
            return 0

        results = json.loads(stdout)  # {aweme_id: {video_play_url, audio_url}}
        success = 0
        for idx, v in missing:
            vid = v.get("id")
            if vid in results and results[vid]:
                info = results[vid]
                if isinstance(info, dict):
                    if info.get("video_play_url"):
                        videos[idx]["video_play_url"] = info["video_play_url"]
                    if info.get("audio_url"):
                        videos[idx]["audio_url"] = info["audio_url"]
                    if info.get("video_play_url") or info.get("audio_url"):
                        success += 1
                elif isinstance(info, str) and info:
                    # 兼容旧格式
                    videos[idx]["audio_url"] = info
                    success += 1

        return success

    except Exception as e:
        print(f"  f2 详情获取异常: {e}")
        return 0
