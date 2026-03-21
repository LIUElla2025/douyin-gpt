"""语音转文字模块 - 使用 OpenAI Whisper API（云端转录）

优化特性：
- 并发转录（ThreadPoolExecutor，默认 5 workers）
- 失败自动重试（最多 2 次，指数退避）
- 大文件分片（>24MB 用 pydub 切片后分段转录再拼接）
"""

import json
import os
import time
import httpx
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
from config import OPENAI_API_KEY, TRANSCRIPTS_DIR, sanitize_id

_PROXY = os.getenv("WHISPER_PROXY", "")  # 为空则不使用代理
_MAX_WORKERS = 5
_MAX_RETRIES = 2
_CHUNK_SIZE_MB = 23  # 分片大小上限（MB），留 1MB 余量


def _make_client():
    """每个线程创建独立的 client，避免连接池冲突"""
    http_kwargs = {"timeout": httpx.Timeout(300, connect=60)}
    if _PROXY:
        http_kwargs["proxy"] = _PROXY
    return OpenAI(
        api_key=OPENAI_API_KEY,
        http_client=httpx.Client(**http_kwargs),
    )


def _call_whisper(client: OpenAI, audio_path: Path) -> dict:
    """调用 Whisper API 转录单个文件（≤25MB）"""
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
        start = seg.start if hasattr(seg, "start") else seg["start"]
        end = seg.end if hasattr(seg, "end") else seg["end"]
        text = seg.text if hasattr(seg, "text") else seg["text"]
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


def _split_audio(audio_path: Path) -> list[Path]:
    """将大音频文件切片为多个 ≤23MB 的临时文件"""
    try:
        from pydub import AudioSegment
    except ImportError:
        raise ImportError("pydub 未安装，无法分片大文件。请运行: pip install pydub")

    audio = AudioSegment.from_file(str(audio_path))
    total_size = audio_path.stat().st_size
    total_duration_ms = len(audio)

    # 按文件大小比例估算每片时长
    chunk_duration_ms = int(total_duration_ms * (_CHUNK_SIZE_MB * 1024 * 1024) / total_size)
    chunk_duration_ms = max(chunk_duration_ms, 30_000)  # 至少 30 秒

    chunks = []
    start = 0
    tmp_dir = Path(tempfile.mkdtemp(prefix="whisper_chunks_"))

    while start < total_duration_ms:
        end = min(start + chunk_duration_ms, total_duration_ms)
        chunk = audio[start:end]
        chunk_path = tmp_dir / f"chunk_{len(chunks):03d}.mp3"
        chunk.export(str(chunk_path), format="mp3")

        # 如果导出的文件仍然太大，缩短时长重试
        if chunk_path.stat().st_size > 24 * 1024 * 1024 and end - start > 60_000:
            chunk_path.unlink()
            chunk_duration_ms = int(chunk_duration_ms * 0.7)
            continue

        chunks.append(chunk_path)
        start = end

    return chunks


def _merge_transcripts(parts: list[dict], chunk_offsets_sec: list[float]) -> dict:
    """合并多个分片的转录结果"""
    all_text = []
    all_segments = []

    for part, offset in zip(parts, chunk_offsets_sec):
        all_text.append(part["text"])
        for seg in part.get("segments", []):
            all_segments.append({
                "start": round(seg["start"] + offset, 2),
                "end": round(seg["end"] + offset, 2),
                "text": seg["text"],
            })

    return {
        "text": " ".join(all_text),
        "segments": all_segments,
        "language": parts[0].get("language", "zh") if parts else "zh",
    }


def transcribe_audio(audio_path: str | Path) -> dict:
    """转录单个音频，支持重试和大文件分片"""
    audio_path = Path(audio_path)
    file_size_mb = audio_path.stat().st_size / 1024 / 1024

    # 大文件：分片转录
    if file_size_mb > 24:
        return _transcribe_large_file(audio_path)

    # 普通文件：直接转录 + 重试
    client = _make_client()
    last_error = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return _call_whisper(client, audio_path)
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                wait = 2 ** attempt  # 1s, 2s
                print(f"  转录重试 ({attempt+1}/{_MAX_RETRIES}): {audio_path.name} - {e}")
                time.sleep(wait)
    raise last_error


def _transcribe_large_file(audio_path: Path) -> dict:
    """分片转录大文件"""
    file_size_mb = audio_path.stat().st_size / 1024 / 1024
    print(f"  大文件分片: {audio_path.name} ({file_size_mb:.1f}MB)")

    chunks = _split_audio(audio_path)
    print(f"  切分为 {len(chunks)} 个片段")

    # 计算每个分片的时间偏移
    try:
        from pydub import AudioSegment
        offsets = []
        cumulative = 0.0
        for chunk_path in chunks:
            offsets.append(cumulative)
            chunk_audio = AudioSegment.from_file(str(chunk_path))
            cumulative += len(chunk_audio) / 1000.0
    except Exception:
        # fallback: 均匀分布
        offsets = [0.0] * len(chunks)

    # 逐片转录（分片内已经够小，不需要并发）
    client = _make_client()
    parts = []
    for i, chunk_path in enumerate(chunks):
        last_error = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                result = _call_whisper(client, chunk_path)
                parts.append(result)
                break
            except Exception as e:
                last_error = e
                if attempt < _MAX_RETRIES:
                    time.sleep(2 ** attempt)
        else:
            print(f"  分片 {i+1}/{len(chunks)} 转录失败: {last_error}")
            parts.append({"text": "", "segments": [], "language": "zh"})

    # 清理临时文件
    tmp_parent = chunks[0].parent if chunks else None
    for chunk_path in chunks:
        chunk_path.unlink(missing_ok=True)
    if tmp_parent:
        tmp_parent.rmdir()

    return _merge_transcripts(parts, offsets)


def transcribe_batch(
    videos: list[dict],
    progress_callback=None,
    save_callback=None,
) -> list[dict]:
    """并发批量转录，支持断点续传

    Args:
        videos: 视频列表（需有 audio_path 字段）
        progress_callback: 进度回调 (done, total, title)
        save_callback: 每完成 N 个后调用保存
    Returns:
        更新了 transcript 字段的视频列表
    """
    # 找出需要转录的视频（跳过已有结果的 = 断点续传）
    to_transcribe = []
    for i, v in enumerate(videos):
        t = v.get("transcript")
        if t and isinstance(t, dict) and t.get("text"):
            continue  # 已有转录结果，跳过
        audio_path = v.get("audio_path")
        if audio_path and Path(audio_path).exists():
            to_transcribe.append((i, v))

    if not to_transcribe:
        return videos

    total = len(to_transcribe)
    done = 0
    success = 0
    failed = 0

    def _worker(index_video):
        idx, video = index_video
        client = _make_client()
        audio_path = Path(video["audio_path"])
        file_size_mb = audio_path.stat().st_size / 1024 / 1024

        if file_size_mb > 24:
            return idx, _transcribe_large_file(audio_path)

        last_error = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return idx, _call_whisper(client, audio_path)
            except Exception as e:
                last_error = e
                if attempt < _MAX_RETRIES:
                    time.sleep(2 ** attempt)
        raise last_error

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_worker, item): item for item in to_transcribe}

        for future in as_completed(futures):
            original_idx, video = futures[future]
            done += 1

            try:
                result_idx, transcript = future.result()
                videos[result_idx]["transcript"] = transcript
                success += 1
            except Exception as e:
                title = video.get("title", "")[:30]
                print(f"  转录失败 [{done}/{total}]: {title} - {type(e).__name__}: {e}")
                videos[original_idx]["transcript"] = None
                failed += 1

            if progress_callback:
                title = video.get("title", "")[:30]
                progress_callback(done, total, title)

            # 每 5 个保存一次中间结果
            if save_callback and done % 5 == 0:
                save_callback()

    print(f"  转录完成: 成功 {success}, 失败 {failed}, 跳过 {len(videos) - total - (len(videos) - len([v for v in videos if v.get('audio_path') and Path(v['audio_path']).exists()]))}, 总计 {total}")
    return videos


def save_transcripts(videos: list[dict], douyin_id: str) -> Path:
    """保存转录结果到 JSON"""
    safe_id = sanitize_id(douyin_id)
    output_path = TRANSCRIPTS_DIR / f"{safe_id}_transcripts.json"

    save_data = []
    for v in videos:
        save_data.append({
            "id": v.get("id"),
            "title": v.get("title"),
            "raw_title": v.get("raw_title"),
            "author": v.get("author"),
            "creator_name": v.get("creator_name"),
            "create_time": v.get("create_time"),
            "duration": v.get("duration"),
            "digg_count": v.get("digg_count"),
            "url": v.get("url"),
            "video_play_url": v.get("video_play_url"),
            "audio_url": v.get("audio_url"),
            # 不保存 audio_path — 临时文件，转录后删除
            "transcript": v.get("transcript"),
        })

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)

    return output_path


def load_transcripts(douyin_id: str) -> list[dict] | None:
    """从文件加载已有的转录结果"""
    safe_id = sanitize_id(douyin_id)
    path = TRANSCRIPTS_DIR / f"{safe_id}_transcripts.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None
