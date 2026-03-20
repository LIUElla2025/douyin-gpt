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


def _save_cursor(checkpoint_path: str, cursor_value):
    """保存翻页 cursor，下次从这里继续"""
    if not checkpoint_path:
        return
    cursor_path = checkpoint_path.replace(".jsonl", ".cursor.json")
    with open(cursor_path, "w") as f:
        json.dump({"max_cursor": cursor_value}, f)


def _load_cursor(checkpoint_path: str):
    """加载上次保存的翻页 cursor"""
    if not checkpoint_path:
        return 0
    cursor_path = checkpoint_path.replace(".jsonl", ".cursor.json")
    if not os.path.exists(cursor_path):
        return 0
    try:
        with open(cursor_path, "r") as f:
            data = json.load(f)
            return data.get("max_cursor", 0)
    except Exception:
        return 0


async def fetch_videos(sec_uid: str, cookie: str, max_videos: int = None,
                       keywords: list[str] = None, checkpoint_path: str = None):
    # 把所有日志输出重定向到 stderr，保持 stdout 干净（只输出 JSON）
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    logging.getLogger("f2").setLevel(logging.WARNING)
    logging.getLogger().setLevel(logging.WARNING)
    os.environ.setdefault("F2_BARK_KEY", "")

    from f2.apps.douyin.handler import DouyinHandler
    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.model import UserPost
    from f2.apps.douyin.filter import UserPostFilter

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

    all_videos = list(existing_videos)
    seen_ids = set(existing_ids)
    matched_videos = [v for v in existing_videos if keywords and _match_keyword(v, keywords)]

    # ─── 直接控制翻页（不依赖 handler 的 generator）───
    max_target = max_videos or float("inf")
    # 从上次保存的 cursor 继续翻页，而不是每次从 0 开始
    saved_cursor = _load_cursor(checkpoint_path)
    max_cursor = saved_cursor
    if saved_cursor:
        print(f"f2_info: 从上次 cursor={saved_cursor} 继续翻页", file=sys.stderr)
    page_size = 20
    consecutive_empty = 0
    consecutive_all_dup = 0
    page_num = 0
    retry_count = 0
    max_retries = 3  # 每个 cursor 位置最多重试 3 次

    while len(all_videos) < max_target:
        # 请求一页数据
        try:
            async with DouyinCrawler(kwargs) as crawler:
                params = UserPost(
                    max_cursor=max_cursor,
                    count=page_size,
                    sec_user_id=sec_uid,
                )
                response = await crawler.fetch_user_post(params)
                page_filter = UserPostFilter(response)
        except Exception as e:
            retry_count += 1
            if retry_count > max_retries:
                print(f"f2_info: 连续 {retry_count} 次请求失败，停止: {e}", file=sys.stderr)
                break
            wait = 10 * retry_count
            print(f"f2_info: 请求失败({e})，{wait}秒后重试...", file=sys.stderr)
            sys.stderr.flush()
            await asyncio.sleep(wait)
            continue

        retry_count = 0  # 请求成功，重置重试计数
        page_num += 1

        aweme_ids = page_filter.aweme_id
        has_more = page_filter.has_more
        new_cursor = page_filter.max_cursor

        if not aweme_ids or (isinstance(aweme_ids, list) and len(aweme_ids) == 0):
            consecutive_empty += 1
            print(f"f2_debug: 第 {page_num} 页空数据, has_more={has_more}, cursor={max_cursor}", file=sys.stderr)
            if not has_more or consecutive_empty >= 5:
                # 即使 has_more=False，如果我们获取的数量远少于预期，等一会儿再试
                if len(all_videos) < total_videos * 0.8 and retry_count < max_retries:
                    retry_count += 1
                    wait = 15 * retry_count
                    print(f"f2_info: 仅获取 {len(all_videos)}/{total_videos}，等 {wait}秒后从 cursor={max_cursor} 重试...", file=sys.stderr)
                    sys.stderr.flush()
                    await asyncio.sleep(wait)
                    consecutive_empty = 0
                    continue
                break
            # has_more=True 但空数据，用新 cursor 继续
            if new_cursor and new_cursor != max_cursor:
                max_cursor = new_cursor
            await asyncio.sleep(3)
            continue

        consecutive_empty = 0

        if not isinstance(aweme_ids, list):
            aweme_ids = [aweme_ids]

        descs = page_filter.desc if isinstance(page_filter.desc, list) else [page_filter.desc]
        nicknames = page_filter.nickname if isinstance(page_filter.nickname, list) else [page_filter.nickname]
        create_times = page_filter.create_time if isinstance(page_filter.create_time, list) else [page_filter.create_time]
        durations = page_filter.video_duration if isinstance(page_filter.video_duration, list) else [page_filter.video_duration]
        music_urls = page_filter.music_play_url if isinstance(page_filter.music_play_url, list) else [page_filter.music_play_url]
        play_addrs = page_filter.video_play_addr if isinstance(page_filter.video_play_addr, list) else [page_filter.video_play_addr]

        def _safe_get(lst, idx, default=""):
            try:
                return lst[idx] if isinstance(lst, list) and idx < len(lst) else default
            except (IndexError, TypeError):
                return default

        page_new = []
        for i, vid in enumerate(aweme_ids):
            vid_str = str(vid)
            if vid_str in seen_ids:
                continue

            desc = _safe_get(descs, i, "无标题")
            title = re.sub(r'#\S+', '', desc).strip() or "无标题"
            duration = _safe_get(durations, i, 0)
            if isinstance(duration, (int, float)) and duration > 10000:
                duration = duration // 1000

            # 提取 URL — f2 返回的 url 字段可能是 list，需要取第一个
            raw_audio_url = _safe_get(music_urls, i, "")
            if isinstance(raw_audio_url, list):
                raw_audio_url = raw_audio_url[0] if raw_audio_url else ""

            raw_video_url = _safe_get(play_addrs, i, "")
            if isinstance(raw_video_url, list):
                raw_video_url = raw_video_url[0] if raw_video_url else ""

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
                "audio_url": raw_audio_url,
                "video_play_url": raw_video_url,
                "creator_name": creator_name,
            }
            page_new.append(video)
            seen_ids.add(vid_str)

        # 全部是已知视频
        if not page_new:
            consecutive_all_dup += len(aweme_ids)
            if consecutive_all_dup >= 200:
                print(f"f2_info: 连续 {consecutive_all_dup} 个重复视频，完成", file=sys.stderr)
                break
        else:
            consecutive_all_dup = 0
            all_videos.extend(page_new)
            _append_checkpoint(checkpoint_path, page_new)

            # 先发标题，再发进度
            if keywords:
                new_matches = [v for v in page_new if _match_keyword(v, keywords)]
                match_base = len(matched_videos)
                for j, v in enumerate(new_matches):
                    idx = match_base + j + 1
                    print(f"f2_title: {idx}. {v['title'][:60]}", file=sys.stderr)
                matched_videos.extend(new_matches)
                print(f"f2_progress: 已扫描 {len(seen_ids)}/{total_videos} | 匹配 {len(matched_videos)} 个", file=sys.stderr)
            else:
                total_got = len(all_videos)
                base_idx = total_got - len(page_new)
                for j, v in enumerate(page_new):
                    idx = base_idx + j + 1
                    print(f"f2_title: {idx}. {v['title'][:60]}", file=sys.stderr)
                print(f"f2_progress: 已获取 {total_got}/{total_videos} 个视频", file=sys.stderr)

            sys.stderr.flush()

        # 翻页：更新 cursor
        if not has_more:
            # API 说没有更多了 — 保存当前 cursor，下次从这里继续
            if new_cursor:
                _save_cursor(checkpoint_path, new_cursor)
            if len(all_videos) < total_videos * 0.8:
                print(f"f2_info: 本轮获取 {len(all_videos)}/{total_videos}（{len(all_videos)*100//max(total_videos,1)}%），cursor 已保存，下次继续", file=sys.stderr)
            else:
                print(f"f2_info: 全部视频获取完成", file=sys.stderr)
            break

        if new_cursor and new_cursor != max_cursor:
            max_cursor = new_cursor
            # 每页都保存 cursor，即使中途中断也能恢复
            _save_cursor(checkpoint_path, new_cursor)
        else:
            # cursor 没变化，保存并停止
            _save_cursor(checkpoint_path, max_cursor)
            print(f"f2_debug: cursor 未变化 ({max_cursor})，停止", file=sys.stderr)
            break

        # 翻页间隔（10秒，降低风控）
        await asyncio.sleep(10)

    # 如果已经获取到足够多，或已到末尾，重置 cursor 让下次从头扫
    if len(all_videos) >= total_videos * 0.95:
        _save_cursor(checkpoint_path, 0)
        print(f"f2_info: 已获取 95%+ 视频，cursor 重置", file=sys.stderr)

    # 返回结果
    result = matched_videos if keywords else all_videos
    print(f"f2_done: 共 {len(result)} 个视频（扫描 {page_num} 页）", file=sys.stderr)
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
