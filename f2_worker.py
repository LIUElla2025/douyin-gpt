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
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    logging.getLogger("f2").setLevel(logging.WARNING)
    logging.getLogger().setLevel(logging.WARNING)
    os.environ.setdefault("F2_BARK_KEY", "")

    from f2.apps.douyin.handler import DouyinHandler

    kwargs = {
        "headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
            "Referer": "https://www.douyin.com/",
        },
        "cookie": cookie,
        "proxies": {"http://": None, "https://": None},
        "timeout": 8,  # 翻页间隔秒数，比默认5s长，降低被风控概率
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
    seen_ids = set(existing_ids)  # 全局去重集合
    matched_videos = [v for v in existing_videos if keywords and _match_keyword(v, keywords)]

    # ─── 重试循环：如果获取数量远少于预期，自动重试（最多3轮）───
    max_attempts = 3
    for attempt in range(max_attempts):
        scanned = 0
        new_count = 0
        duplicate_streak = 0
        empty_page_count = 0
        round_new = 0  # 本轮新增数量

        if attempt > 0:
            wait = 15 * attempt  # 第2轮等15秒，第3轮等30秒
            print(f"f2_info: 第 {attempt+1} 轮重试，等待 {wait} 秒后继续获取...", file=sys.stderr)
            sys.stderr.flush()
            await asyncio.sleep(wait)
            # 重新创建 handler（刷新连接）
            handler = DouyinHandler(kwargs)

        async for page_filter in handler.fetch_user_post_videos(
            sec_user_id=sec_uid,
            max_counts=max_videos,
        ):
            aweme_ids = page_filter.aweme_id
            # 诊断日志
            has_more = getattr(page_filter, 'has_more', None)
            page_size = len(aweme_ids) if isinstance(aweme_ids, list) else (1 if aweme_ids else 0)
            print(f"f2_debug: 本页返回 {page_size} 个视频, has_more={has_more}", file=sys.stderr)
            sys.stderr.flush()

            if not aweme_ids:
                empty_page_count += 1
                print(f"f2_debug: 第 {empty_page_count} 个空页，继续翻页...", file=sys.stderr)
                if empty_page_count >= 5:
                    print(f"f2_debug: 连续 {empty_page_count} 个空页，停止获取", file=sys.stderr)
                    break
                continue
            empty_page_count = 0

            descs = page_filter.desc if isinstance(page_filter.desc, list) else [page_filter.desc]
            nicknames = page_filter.nickname if isinstance(page_filter.nickname, list) else [page_filter.nickname]
            create_times = page_filter.create_time if isinstance(page_filter.create_time, list) else [page_filter.create_time]
            durations = page_filter.video_duration if isinstance(page_filter.video_duration, list) else [page_filter.video_duration]
            music_urls = page_filter.music_play_url if isinstance(page_filter.music_play_url, list) else [page_filter.music_play_url]
            play_addrs = page_filter.video_play_addr if isinstance(page_filter.video_play_addr, list) else [page_filter.video_play_addr]

            if not isinstance(aweme_ids, list):
                aweme_ids = [aweme_ids]

            page_new = []
            for i, vid in enumerate(aweme_ids):
                def _safe_get(lst, idx, default=""):
                    try:
                        return lst[idx] if isinstance(lst, list) and idx < len(lst) else default
                    except (IndexError, TypeError):
                        return default

                vid_str = str(vid)
                # 全局去重：跳过已获取过的视频（含断点 + 之前轮次）
                if vid_str in seen_ids:
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
                page_new.append(video)
                seen_ids.add(vid_str)

            # 如果整页都是已知视频，计入重复
            if not page_new:
                duplicate_streak += len(aweme_ids)
                scanned += len(aweme_ids)
                if duplicate_streak >= 200:
                    print(f"f2_info: 连续 {duplicate_streak} 个重复，本轮完成", file=sys.stderr)
                    break
                continue
            else:
                duplicate_streak = 0

            all_videos.extend(page_new)
            round_new += len(page_new)
            new_count += len(page_new)
            scanned += len(aweme_ids)

            # 增量写入 checkpoint
            _append_checkpoint(checkpoint_path, page_new)

            # 先发标题，再发进度
            if keywords:
                new_matches = [v for v in page_new if _match_keyword(v, keywords)]
                match_base = len(matched_videos)
                for j, v in enumerate(new_matches):
                    idx = match_base + j + 1
                    print(f"f2_title: {idx}. {v['title'][:60]}", file=sys.stderr)
                matched_videos.extend(new_matches)
                print(f"f2_progress: 已扫描 {scanned}/{total_videos} | 匹配 {len(matched_videos)} 个", file=sys.stderr)
            else:
                total_got = len(all_videos)
                base_idx = total_got - len(page_new)
                for j, v in enumerate(page_new):
                    idx = base_idx + j + 1
                    print(f"f2_title: {idx}. {v['title'][:60]}", file=sys.stderr)
                print(f"f2_progress: 已获取 {total_got}/{total_videos} 个视频", file=sys.stderr)

            sys.stderr.flush()

        # ─── 判断是否需要重试 ───
        got_count = len(all_videos)
        if total_videos > 0 and got_count < total_videos * 0.8 and round_new > 0:
            # 获取不足 80%，且本轮确实拿到了新数据（非全重复），继续重试
            print(f"f2_info: 已获取 {got_count}/{total_videos}（{got_count*100//total_videos}%），尝试继续获取...", file=sys.stderr)
            sys.stderr.flush()
            continue
        elif round_new == 0 and attempt < max_attempts - 1:
            # 本轮一个新视频都没获取到，也重试一次
            print(f"f2_info: 本轮未获取到新视频，重试...", file=sys.stderr)
            sys.stderr.flush()
            continue
        else:
            break  # 够了或已达最大重试次数

    # 返回结果
    result = matched_videos if keywords else all_videos
    print(f"f2_done: 共 {len(result)} 个视频（{max_attempts} 轮内扫描完成）", file=sys.stderr)
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
