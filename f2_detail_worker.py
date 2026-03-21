"""用 f2 的详情 API 获取视频的实时播放直链

用法:
  批量模式: echo '["id1","id2"]' | python f2_detail_worker.py
  单视频模式: python f2_detail_worker.py --single <aweme_id>
  cookie 通过环境变量 DOUYIN_COOKIE 传入
输出: JSON 到 stdout，进度到 stderr
"""

import asyncio
import json
import logging
import os
import sys


async def _fetch_one_video(crawler_cls, filter_cls, detail_cls, kwargs, vid):
    """获取单个视频的实时 URL（带重试）"""
    for attempt in range(3):
        try:
            async with crawler_cls(kwargs) as crawler:
                params = detail_cls(aweme_id=vid)
                response = await crawler.fetch_post_detail(params)
                detail = filter_cls(response)

                # 视频播放直链（包含真正的口述音频）
                video_play_url = ""
                play_addr = detail.video_play_addr
                while isinstance(play_addr, list) and play_addr:
                    play_addr = play_addr[0]
                if isinstance(play_addr, str) and play_addr:
                    video_play_url = play_addr

                # 背景音乐链接（仅记录，不用于转录）
                audio_url = detail.music_play_url or ""
                if isinstance(audio_url, list):
                    audio_url = audio_url[0] if audio_url else ""

                return {"video_play_url": video_play_url, "audio_url": audio_url}
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(3 * (attempt + 1))
            else:
                print(f"detail_error: {vid} - {e}", file=sys.stderr)
    return None


async def fetch_single(vid: str, cookie: str) -> dict:
    """获取单个视频的实时 URL"""
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

    result = await _fetch_one_video(DouyinCrawler, PostDetailFilter, PostDetail, kwargs, vid)
    return result or {}


async def fetch_batch(video_ids: list[str], cookie: str) -> dict:
    """批量获取视频的实时 URL"""
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
        result = await _fetch_one_video(DouyinCrawler, PostDetailFilter, PostDetail, kwargs, vid)
        if result and result.get("video_play_url"):
            results[vid] = result
            success += 1

        if (i + 1) % 3 == 0 or i == total - 1:
            print(f"detail_progress: 补全链接 {i+1}/{total}（成功 {success}）", file=sys.stderr)
            sys.stderr.flush()

        await asyncio.sleep(2)

    return results


def main():
    cookie = os.environ.get("DOUYIN_COOKIE", "")
    if not cookie:
        print("错误: 未设置 DOUYIN_COOKIE", file=sys.stderr)
        sys.exit(1)

    real_stdout = sys.stdout
    sys.stdout = sys.stderr

    try:
        # --single 模式：单个视频 ID
        if "--single" in sys.argv:
            idx = sys.argv.index("--single")
            if idx + 1 < len(sys.argv):
                vid = sys.argv[idx + 1]
                result = asyncio.run(fetch_single(vid, cookie))
                sys.stdout = real_stdout
                json.dump(result, real_stdout, ensure_ascii=False)
                return

        # 批量模式：从 stdin 读取
        sys.stdout = real_stdout
        input_data = sys.stdin.read().strip()
        sys.stdout = sys.stderr

        if not input_data:
            sys.stdout = real_stdout
            print("{}")
            return

        video_ids = json.loads(input_data)
        results = asyncio.run(fetch_batch(video_ids, cookie))
    finally:
        sys.stdout = real_stdout

    json.dump(results, real_stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
