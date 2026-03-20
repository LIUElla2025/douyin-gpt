"""用 f2 的详情 API 获取视频的播放直链（用于提取口述音频）

用法: echo '["id1","id2"]' | python f2_detail_worker.py
     cookie 通过环境变量 DOUYIN_COOKIE 传入
输出: JSON {aweme_id: {video_play_url: "...", audio_url: "..."}} 到 stdout
     进度到 stderr
"""

import asyncio
import json
import logging
import os
import sys


async def fetch_video_urls(video_ids: list[str], cookie: str) -> dict:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    logging.getLogger("f2").setLevel(logging.WARNING)
    os.environ.setdefault("F2_BARK_KEY", "")

    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.model import PostDetail
    from f2.apps.douyin.filter import PostDetailFilter

    kwargs = {
        "headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
            "Referer": "https://www.douyin.com/",
        },
        "cookie": cookie,
        "proxies": {"http://": None, "https://": None},
    }

    results = {}
    total = len(video_ids)
    success = 0

    for i, vid in enumerate(video_ids):
        got_it = False
        for attempt in range(3):  # 每个视频最多重试3次
            try:
                async with DouyinCrawler(kwargs) as crawler:
                    params = PostDetail(aweme_id=vid)
                    response = await crawler.fetch_post_detail(params)
                    detail = PostDetailFilter(response)

                    # 优先获取视频播放直链（包含真正的口述音频）
                    video_play_url = ""
                    play_addr = detail.video_play_addr
                    if isinstance(play_addr, list) and play_addr:
                        video_play_url = play_addr[0]
                    elif isinstance(play_addr, str) and play_addr:
                        video_play_url = play_addr

                    # 背景音乐链接（仅作备选）
                    audio_url = detail.music_play_url or ""

                    if video_play_url or audio_url:
                        results[vid] = {
                            "video_play_url": video_play_url,
                            "audio_url": audio_url,
                        }
                        success += 1
                        got_it = True
                        break
                    # API 返回了数据但没有 URL，不重试
                    break
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))  # 5s, 10s
                else:
                    print(f"detail_error: {vid} - {e}", file=sys.stderr)

        if (i + 1) % 3 == 0 or i == total - 1:
            print(f"detail_progress: 补全链接 {i+1}/{total}（成功 {success}）", file=sys.stderr)
            sys.stderr.flush()

        # 间隔避免限流（成功后短间隔，失败后长间隔）
        await asyncio.sleep(2 if got_it else 5)

    return results


def main():
    cookie = os.environ.get("DOUYIN_COOKIE", "")
    if not cookie:
        print("错误: 未设置 DOUYIN_COOKIE", file=sys.stderr)
        sys.exit(1)

    input_data = sys.stdin.read().strip()
    if not input_data:
        print("{}")
        return

    video_ids = json.loads(input_data)

    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        results = asyncio.run(fetch_video_urls(video_ids, cookie))
    finally:
        sys.stdout = real_stdout

    json.dump(results, real_stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
