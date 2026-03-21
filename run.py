#!/usr/bin/env python3
"""抖音博主文稿提取 — 本地 CLI 工具

用法:
  python run.py "抖音分享链接"
  python run.py "抖音分享链接" --keyword "心理学"
  python run.py "抖音分享链接" --max-videos 50
  python run.py "抖音分享链接" --resume    # 恢复上次进度
"""

import argparse
import time
from pathlib import Path

from scraper import (
    get_creator_videos, download_video_audio, save_video_list,
    clear_checkpoint,
)
from transcriber import transcribe_batch, save_transcripts, load_transcripts
from doc_generator import generate_word_doc


def parse_args():
    parser = argparse.ArgumentParser(
        description="抖音博主文稿提取：输入链接 → 输出 Word 文档",
    )
    parser.add_argument("link", help="抖音分享链接、主页链接或抖音号")
    parser.add_argument("--keyword", default="", help="关键词过滤（空格分隔，匹配任一）")
    parser.add_argument("--max-videos", type=int, default=0, help="最多获取几个视频（0=全部）")
    parser.add_argument("--resume", action="store_true", help="恢复上次进度，跳过已转录的视频")
    return parser.parse_args()


def main():
    args = parse_args()
    douyin_id = args.link.strip()
    max_videos = args.max_videos or None

    print("=" * 60)
    print("  抖音博主文稿提取工具")
    print("=" * 60)

    # ─── Step 1: 获取视频列表 ───
    print(f"\n📡 Step 1: 获取视频列表...")

    def on_fetch_progress(p, text):
        clean = text.split("\n---\n")[0]
        print(f"  [{p*100:.0f}%] {clean}")

    videos = get_creator_videos(
        douyin_id,
        max_videos=max_videos,
        progress_callback=on_fetch_progress,
        keyword=args.keyword,
    )

    creator_name = ""
    for v in videos:
        name = v.get("creator_name") or v.get("author") or ""
        if name:
            creator_name = name
            break
    if not creator_name:
        creator_name = douyin_id

    print(f"  ✅ 获取到「{creator_name}」的 {len(videos)} 个视频")
    save_video_list(videos, douyin_id)

    # ─── Step 1.5: 恢复已有转录（--resume 模式）───
    if args.resume:
        prior = load_transcripts(douyin_id)
        if prior:
            prior_map = {}
            for pv in prior:
                vid = pv.get("id")
                if vid and pv.get("transcript"):
                    prior_map[vid] = pv["transcript"]
            merged = 0
            for v in videos:
                vid = v.get("id")
                if vid and vid in prior_map and not v.get("transcript"):
                    v["transcript"] = prior_map[vid]
                    merged += 1
            if merged:
                print(f"  🔄 从上次恢复了 {merged} 条已有转录")

    # ─── Step 2: 逐个下载音频（实时获取新鲜 URL）───
    need_audio = [v for v in videos
                  if not (v.get("transcript") and isinstance(v.get("transcript"), dict)
                          and v["transcript"].get("text"))
                  and v.get("id")]

    print(f"\n🔊 Step 2: 下载 {len(need_audio)} 个视频的音频...")
    print("  （每个视频实时获取新鲜URL → 下载视频 → ffmpeg提取音频）")

    downloaded_count = 0
    skipped_count = 0
    for i, video in enumerate(need_audio):
        title = video.get("title", "")[:40]
        audio_path = download_video_audio(video, i)
        video["audio_path"] = str(audio_path) if audio_path else None
        video["downloaded"] = audio_path is not None

        if audio_path:
            downloaded_count += 1
            print(f"  ✅ [{i+1}/{len(need_audio)}] {title}")
        else:
            skipped_count += 1
            print(f"  ⬜ [{i+1}/{len(need_audio)}] {title} (跳过)")

        # 下载间隔，防止限流
        if i < len(need_audio) - 1:
            time.sleep(2)

    print(f"  📊 成功 {downloaded_count}，跳过 {skipped_count}")

    # ─── Step 3: 并行转录（5路 Whisper）───
    need_transcribe = [v for v in videos
                       if v.get("audio_path") and Path(v["audio_path"]).exists()
                       and not (v.get("transcript") and isinstance(v.get("transcript"), dict)
                                and v["transcript"].get("text"))]

    if need_transcribe:
        print(f"\n🤖 Step 3: 转录 {len(need_transcribe)} 个音频（5路并行）...")
        start_time = time.time()

        def on_transcribe(done, total, title):
            elapsed = int(time.time() - start_time)
            mins, secs = divmod(elapsed, 60)
            print(f"  [{done}/{total}] {title[:40]} ({mins}分{secs:02d}秒)")

        videos = transcribe_batch(
            videos,
            progress_callback=on_transcribe,
            save_callback=lambda: save_transcripts(videos, douyin_id),
        )
    else:
        print(f"\n🤖 Step 3: 无需转录（全部已有转录或下载失败）")

    # ─── Step 3.5: GPT 校对（加标点、去错别字和口水词）───
    to_polish = [v for v in videos
                 if v.get("transcript") and isinstance(v["transcript"], dict)
                 and v["transcript"].get("text")
                 and not v["transcript"]["text"].startswith("[视频描述]")
                 and not v["transcript"].get("polished")]
    if to_polish:
        print(f"\n✍️  Step 3.5: GPT 校对 {len(to_polish)} 篇文稿（加标点、去口水词）...")
        from config import OPENAI_API_KEY
        import httpx
        from openai import OpenAI
        import os

        proxy = os.getenv("WHISPER_PROXY", "")
        http_kwargs = {"timeout": httpx.Timeout(120, connect=30)}
        if proxy:
            http_kwargs["proxy"] = proxy
        gpt_client = OpenAI(api_key=OPENAI_API_KEY, http_client=httpx.Client(**http_kwargs))

        for i, v in enumerate(to_polish):
            raw_text = v["transcript"]["text"]
            title = v.get("title", "")[:30]
            try:
                resp = gpt_client.chat.completions.create(
                    model="gpt-4.1-mini",
                    max_tokens=16384,
                    messages=[
                        {"role": "system", "content": "你是一个中文文稿校对员。请对以下语音转录文稿进行校对：\n1. 添加正确的中文标点符号（句号、逗号、问号等）\n2. 修正明显的错别字（语音识别错误）\n3. 删除口水词（嗯、啊、那个、就是说、然后呢等无意义的填充词）\n4. 保持原文意思和说话风格不变\n5. 不要添加、删除或改写任何实质内容\n6. 直接输出校对后的文字，不要加任何说明"},
                        {"role": "user", "content": raw_text},
                    ],
                )
                polished = resp.choices[0].message.content.strip()
                if polished and len(polished) > len(raw_text) * 0.3:
                    v["transcript"]["text"] = polished
                    v["transcript"]["polished"] = True
                print(f"  ✅ [{i+1}/{len(to_polish)}] {title}")
            except Exception as e:
                print(f"  ⬜ [{i+1}/{len(to_polish)}] {title} (校对失败: {e})")

            # 每 10 个保存一次
            if (i + 1) % 10 == 0:
                save_transcripts(videos, douyin_id)

        save_transcripts(videos, douyin_id)

    # 没有转录的视频用标题作为 fallback
    whisper_count = 0
    desc_count = 0
    for v in videos:
        t = v.get("transcript")
        if t and isinstance(t, dict) and t.get("text"):
            whisper_count += 1
        else:
            desc = v.get("title", "").strip()
            if desc and desc != "无标题":
                v["transcript"] = {
                    "text": f"[视频描述] {desc}",
                    "segments": [],
                    "language": "zh",
                    "source": "douyin_desc",
                }
                desc_count += 1

    # ─── Step 4: 生成 Word 文档 ───
    print(f"\n📄 Step 4: 生成 Word 文档...")
    doc_path = generate_word_doc(videos, creator_name, douyin_id)
    print(f"  ✅ 已保存: {doc_path}")

    # ─── Step 5: 清理 ───
    cleaned = 0
    for v in videos:
        ap = v.get("audio_path")
        if ap:
            p = Path(ap)
            if p.exists():
                p.unlink(missing_ok=True)
                cleaned += 1
            v["audio_path"] = None
    clear_checkpoint(douyin_id)
    save_transcripts(videos, douyin_id)
    if cleaned:
        print(f"  🧹 已清理 {cleaned} 个临时音频文件")

    # ─── 完成 ───
    print(f"\n{'=' * 60}")
    print(f"  ✅ 完成！")
    print(f"  博主: {creator_name}")
    print(f"  视频: {len(videos)} 个")
    print(f"  转录: {whisper_count} 个（Whisper）+ {desc_count} 个（标题）")
    print(f"  文档: {doc_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
