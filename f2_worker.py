"""独立进程运行 f2 获取博主视频列表 — 避免 Streamlit 线程冲突

用法: python f2_worker.py <sec_uid> [max_videos] [--keyword "关键词1 关键词2"] [--checkpoint /path/to/file.jsonl]
     cookie 通过环境变量 DOUYIN_COOKIE 传入
输出: JSON 到 stdout
     进度信息到 stderr（供 UI 实时显示）
     增量数据到 checkpoint 文件（每页追加写入 JSONL，中断不丢失）
"""

import asyncio
import json
import logging
import os
import re
import sys


def _match_keyword(video: dict, keywords: list[str]) -> bool:
    """检查视频是否匹配任一关键词（标题 + 原始标题含#标签）"""
    title = video.get("title", "") or ""
    raw_title = video.get("raw_title", "") or ""
    return any(kw in title or kw in raw_title for kw in keywords)


def _load_checkpoint(checkpoint_path: str) -> tuple[list[dict], set[str]]:
    """加载已有的 checkpoint 数据，返回 (已获取的视频列表, 已知ID集合)"""
    videos = []
    seen_ids = set()
    if not checkpoint_path or not os.path.exists(checkpoint_path):
        return videos, seen_ids
    try:
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                video = json.loads(line)
                vid = video.get("id", "")
                if vid and vid not in seen_ids:
                    videos.append(video)
                    seen_ids.add(vid)
    except Exception as e:
        print(f"f2_info: checkpoint 加载失败({e})，从头开始", file=sys.stderr)
        return [], set()
    return videos, seen_ids


def _append_checkpoint(checkpoint_path: str, videos: list[dict]):
    """追加写入视频到 checkpoint 文件（JSONL 格式，每行一个）"""
    if not checkpoint_path:
        return
    with open(checkpoint_path, "a", encoding="utf-8") as f:
        for v in videos:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")


async def fetch_videos(sec_uid: str, cookie: str, max_videos: int = None,
                       keywords: list[str] = None, checkpoint_path: str = None):
    # 把所有日志输出重定向到 stderr，保持 stdout 干净（只输出 JSON）
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    logging.getLogger("f2").setLevel(logging.INFO)
    logging.getLogger().setLevel(logging.INFO)
    os.environ.setdefault("F2_BARK_KEY", "")

    from f2.apps.douyin.handler import DouyinHandler

    kwargs = {
        "headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
            "Referer": "https://www.douyin.com/",
        },
        "cookie": cookie,
        "proxies": {"http://": None, "https://": None},
    }
    handler = DouyinHandler(kwargs)

    profile = await handler.fetch_user_profile(sec_user_id=sec_uid)
    creator_name = profile.nickname or ""
    total_videos = profile.aweme_count or 0

    # 加载断点数据
    existing_videos, existing_ids = _load_checkpoint(checkpoint_path)
    if existing_videos:
        print(f"f2_info: 从断点恢复，已有 {len(existing_videos)} 个视频", file=sys.stderr)

    if keywords:
        print(f"f2_info: 博主「{creator_name}」共 {total_videos} 个视频，搜索关键词: {' '.join(keywords)}", file=sys.stderr)
    else:
        print(f"f2_info: 博主「{creator_name}」共 {total_videos} 个视频", file=sys.stderr)
    sys.stderr.flush()

    all_videos = list(existing_videos)  # 从断点数据开始
    matched_videos = [v for v in existing_videos if keywords and _match_keyword(v, keywords)]
    scanned = 0
    new_count = 0
    duplicate_streak = 0  # 连续重复计数，用于判断是否已到达断点位置
    empty_page_count = 0  # 连续空页计数

    async for page_filter in handler.fetch_user_post_videos(
        sec_user_id=sec_uid,
        max_counts=max_videos,
    ):
        aweme_ids = page_filter.aweme_id
        # 诊断日志：每页返回的数据量和翻页状态
        has_more = getattr(page_filter, 'has_more', None)
        page_size = len(aweme_ids) if isinstance(aweme_ids, list) else (1 if aweme_ids else 0)
        print(f"f2_info: 本页返回 {page_size} 个视频, has_more={has_more}", file=sys.stderr)
        sys.stderr.flush()

        if not aweme_ids:
            empty_page_count += 1
            print(f"f2_info: 第 {empty_page_count} 个空页，继续翻页...", file=sys.stderr)
            # 连续 5 个空页才放弃，避免因为中间偶尔空页丢失数据
            if empty_page_count >= 5:
                print(f"f2_info: 连续 {empty_page_count} 个空页，停止获取", file=sys.stderr)
                break
            continue
        empty_page_count = 0  # 有数据则重置空页计数

        descs = page_filter.desc if isinstance(page_filter.desc, list) else [page_filter.desc]
        nicknames = page_filter.nickname if isinstance(page_filter.nickname, list) else [page_filter.nickname]
        create_times = page_filter.create_time if isinstance(page_filter.create_time, list) else [page_filter.create_time]
        durations = page_filter.video_duration if isinstance(page_filter.video_duration, list) else [page_filter.video_duration]
        music_urls = page_filter.music_play_url if isinstance(page_filter.music_play_url, list) else [page_filter.music_play_url]
        play_addrs = page_filter.video_play_addr if isinstance(page_filter.video_play_addr, list) else [page_filter.video_play_addr]

        if not isinstance(aweme_ids, list):
            aweme_ids = [aweme_ids]

        page_videos = []
        page_new = []
        for i, vid in enumerate(aweme_ids):
            def _safe_get(lst, idx, default=""):
                try:
                    return lst[idx] if isinstance(lst, list) and idx < len(lst) else default
                except (IndexError, TypeError):
                    return default

            vid_str = str(vid)
            # 跳过断点中已有的视频
            if vid_str in existing_ids:
                continue

            desc = _safe_get(descs, i, "无标题")
            title = re.sub(r'#\S+', '', desc).strip() or "无标题"
            duration = _safe_get(durations, i, 0)
            if isinstance(duration, (int, float)) and duration > 10000:
                duration = duration // 1000

            video = {
                "id": vid_str,
                "title": title,
                "raw_title": desc,
                "url": f"https://www.douyin.com/video/{vid}",
                "share_url": "",
                "create_time": _safe_get(create_times, i, ""),
                "duration": duration,
                "digg_count": 0,
                "author": _safe_get(nicknames, i, creator_name),
                "audio_url": _safe_get(music_urls, i, ""),
                "video_play_url": _safe_get(play_addrs, i, ""),
                "creator_name": creator_name,
            }
            page_videos.append(video)
            page_new.append(video)

        # 如果整页都是已有的视频，计入连续重复
        if not page_new and existing_ids:
            duplicate_streak += len(aweme_ids)
            scanned += len(aweme_ids)
            # 连续 200 个都重复，说明已到断点位置之前的数据都获取过了
            if duplicate_streak >= 200:
                print(f"f2_info: 连续 {duplicate_streak} 个重复，断点续传完成", file=sys.stderr)
                break
            continue
        else:
            duplicate_streak = 0

        all_videos.extend(page_new)
        new_count += len(page_new)
        scanned += len(aweme_ids)

        # 增量写入 checkpoint（每页追加）
        if page_new:
            _append_checkpoint(checkpoint_path, page_new)

        # 先发标题，再发进度 — 确保主线程读到标题和进度同步
        if keywords:
            new_matches = [v for v in page_new if _match_keyword(v, keywords)]
            # 先发所有匹配标题（带全局编号）
            match_base = len(matched_videos)
            for j, v in enumerate(new_matches):
                idx = match_base + j + 1
                print(f"f2_title: {idx}. {v['title'][:60]}", file=sys.stderr)
            matched_videos.extend(new_matches)
            # 再发进度（此时标题已在缓冲区）
            print(f"f2_progress: 已扫描 {scanned}/{total_videos} | 匹配 {len(matched_videos)} 个", file=sys.stderr)
        else:
            total_got = len(all_videos)
            resumed_tag = f"(断点+{new_count})" if existing_videos else ""
            # 先发所有新视频标题（带全局编号）
            base_idx = total_got - len(page_new)
            for j, v in enumerate(page_new):
                idx = base_idx + j + 1
                print(f"f2_title: {idx}. {v['title'][:60]}", file=sys.stderr)
            # 再发进度（此时标题已在缓冲区）
            print(f"f2_progress: 已获取 {total_got}/{total_videos} 个视频{resumed_tag}", file=sys.stderr)

        sys.stderr.flush()

    # 返回：有关键词时只返回匹配的，否则返回全部
    result = matched_videos if keywords else all_videos
    resumed_msg = f"(含断点恢复 {len(existing_videos)} 个)" if existing_videos else ""
    print(f"f2_done: 扫描 {scanned} 个，返回 {len(result)} 个{resumed_msg}", file=sys.stderr)
    sys.stderr.flush()
    return result


def main():
    if len(sys.argv) < 2:
        print("用法: python f2_worker.py <sec_uid> [max_videos] [--keyword '关键词'] [--checkpoint /path/file.jsonl]", file=sys.stderr)
        print("  cookie 通过环境变量 DOUYIN_COOKIE 传入", file=sys.stderr)
        sys.exit(1)

    sec_uid = sys.argv[1]
    cookie = os.environ.get("DOUYIN_COOKIE", "")
    if not cookie:
        print("错误: 未设置 DOUYIN_COOKIE 环境变量", file=sys.stderr)
        sys.exit(1)

    # 解析参数
    max_videos = None
    keywords = None
    checkpoint_path = None
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--keyword" and i + 1 < len(args):
            keywords = args[i + 1].split()
            i += 2
        elif args[i] == "--checkpoint" and i + 1 < len(args):
            checkpoint_path = args[i + 1]
            i += 2
        else:
            try:
                max_videos = int(args[i])
            except ValueError:
                pass
            i += 1

    # 运行 f2 时把 stdout 重定向到 stderr，防止 f2 内部 print 污染 JSON 输出
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        videos = asyncio.run(fetch_videos(sec_uid, cookie, max_videos, keywords, checkpoint_path))
    finally:
        sys.stdout = real_stdout

    json.dump(videos, real_stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
