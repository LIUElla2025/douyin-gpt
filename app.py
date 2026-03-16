"""抖音博主文字稿提取 & GPT 对话工具 — Streamlit 主应用"""

import streamlit as st
from pathlib import Path

from config import OPENAI_API_KEY, TRANSCRIPTS_DIR

_FAVICON = Path(__file__).resolve().parent / "favicon.svg"
from scraper import (
    get_creator_videos, download_all_audios, save_video_list,
    load_checkpoint_videos, clear_checkpoint,
)
from transcriber import transcribe_batch, save_transcripts, load_transcripts
from doc_generator import generate_word_doc
from chat_engine import CreatorChat


def main():
    st.set_page_config(
        page_title="抖音博主 GPT",
        page_icon=str(_FAVICON) if _FAVICON.exists() else "🎬",
        layout="wide",
    )

    st.title("🎬 抖音博主文字稿 & GPT 对话")
    st.caption("输入抖音号或分享链接 → 自动提取所有视频文字稿 → 生成 Word + 博主 GPT 对话")

    # ─── 侧边栏 ───
    with st.sidebar:
        st.header("⚙️ 配置状态")

        if OPENAI_API_KEY:
            st.success("✅ OpenAI API Key 已配置")
        else:
            st.warning("⚠️ OpenAI Key 未设置（GPT 对话需要）")

        st.divider()

        # 显示已处理的博主
        st.header("📁 已处理的博主")
        existing = list(TRANSCRIPTS_DIR.glob("*_transcripts.json"))
        if existing:
            for f in existing:
                creator_id = f.stem.replace("_transcripts", "")
                display_name = creator_id
                try:
                    import json as _json
                    with open(f, "r", encoding="utf-8") as _f:
                        data = _json.load(_f)
                    if data and isinstance(data, list) and isinstance(data[0], dict):
                        display_name = (
                            data[0].get("creator_name")
                            or data[0].get("author")
                            or creator_id
                        )
                except Exception:
                    pass
                if st.button(f"📂 {display_name}", key=f"load_{creator_id}"):
                    st.session_state["loaded_creator_id"] = creator_id
                    st.rerun()
        else:
            st.caption("还没有处理过的博主")

    # ─── 主界面 ───
    tab1, tab2 = st.tabs(["📥 提取文字稿", "💬 博主 GPT 对话"])

    with tab1:
        _render_extraction_tab()

    with tab2:
        _render_chat_tab()


def _render_extraction_tab():
    """文字稿提取界面"""

    douyin_id = st.text_input(
        "抖音号 / 分享链接",
        placeholder="粘贴分享链接 或 输入抖音号",
        help="支持：分享链接（v.douyin.com/xxx）、主页链接（douyin.com/user/xxx）、抖音号",
    )

    douyin_id = douyin_id.strip()

    keyword = st.text_input(
        "关键词过滤（可选）",
        placeholder="例如：偏执 心理学",
        help="只提取标题/标签包含关键词的视频，多个关键词用空格分隔（满足任一即可）。填了关键词会自动获取全部视频再过滤",
    ).strip()

    if not keyword:
        max_videos = st.number_input(
            "获取最近几个视频（0 = 全部）", min_value=0, value=0, step=50,
            help="获取博主最近 N 个视频进行转录。填 0 获取全部视频。",
        )
        if max_videos == 0:
            max_videos = None
    else:
        max_videos = None  # 有关键词时扫描全部视频
        st.caption("🔍 将扫描博主全部视频，只对匹配关键词的视频做转录")

    # ─── 断点续传检测 ───
    has_checkpoint = False
    checkpoint_count = 0
    has_prior_transcripts = False
    prior_count = 0

    if douyin_id:
        # 检查是否有未完成的视频列表获取
        cp_videos = load_checkpoint_videos(douyin_id)
        if cp_videos:
            has_checkpoint = True
            checkpoint_count = len(cp_videos)

        # 检查是否有之前的转录结果（可能音频下载/转录中断）
        prior = load_transcripts(douyin_id)
        if prior:
            has_prior_transcripts = True
            prior_count = len(prior)

    # ─── 操作按钮 ───
    is_running = st.session_state.get("_extraction_running", False)

    col1, col2 = st.columns(2)
    with col1:
        start_fresh = st.button(
            "🚀 开始提取",
            type="primary",
            disabled=not douyin_id or is_running,
        )
    with col2:
        resume = False
        if has_checkpoint or has_prior_transcripts:
            label_parts = []
            if has_checkpoint:
                label_parts.append(f"已获取 {checkpoint_count} 个视频列表")
            if has_prior_transcripts:
                label_parts.append(f"已有 {prior_count} 条转录")
            resume = st.button(
                f"🔄 继续上次（{', '.join(label_parts)}）",
                disabled=not douyin_id or is_running,
            )

    if start_fresh or resume:
        # "开始提取"时清除旧的 checkpoint，从头来
        if start_fresh and not resume:
            clear_checkpoint(douyin_id)
        st.session_state["_extraction_running"] = True
        try:
            _run_extraction(douyin_id, max_videos, keyword, resume=resume)
        finally:
            st.session_state["_extraction_running"] = False

    # 检查是否有从侧边栏加载的博主
    if "loaded_creator_id" in st.session_state:
        loaded_id = st.session_state.pop("loaded_creator_id")
        transcripts = load_transcripts(loaded_id)
        if transcripts:
            # 从数据中提取博主名称
            display_name = loaded_id
            if transcripts and isinstance(transcripts[0], dict):
                display_name = (
                    transcripts[0].get("creator_name")
                    or transcripts[0].get("author")
                    or loaded_id
                )
            st.success(f"已加载「{display_name}」的 {len(transcripts)} 条文字稿")
            st.session_state["current_transcripts"] = transcripts
            st.session_state["current_creator_id"] = loaded_id
            st.session_state["current_creator_name"] = display_name

    # 显示结果
    if "current_transcripts" in st.session_state:
        _show_results()


def _cleanup_audio_files(videos: list[dict]):
    """转录完成后清理音频文件，释放磁盘空间"""
    cleaned = 0
    for v in videos:
        audio_path = v.get("audio_path")
        if audio_path:
            p = Path(audio_path)
            if p.exists():
                p.unlink(missing_ok=True)
                cleaned += 1
            v["audio_path"] = None  # 清除路径引用
    if cleaned:
        print(f"  已清理 {cleaned} 个音频文件")


def _run_extraction(douyin_id: str, max_videos: int = None, keyword: str = "",
                    resume: bool = False):
    """执行完整的提取流程 — 支持断点续传"""

    progress_bar = st.progress(0, text="准备中...")
    status = st.empty()
    live_detail = st.empty()  # 实时显示滚动的视频标题列表

    try:
        # ─── 步骤1: 获取视频列表（断点续传：f2_worker 自动从 checkpoint 恢复）───
        if keyword:
            status.info(f"📡 搜索博主视频中包含「{keyword}」的内容...")
        else:
            status.info("📡 正在获取博主视频列表...")
        progress_bar.progress(0.05, text="获取视频列表...")

        def _on_fetch_progress(p, t):
            # 用 \n---\n 分隔：前半部分是进度文本，后半部分是标题列表
            parts = t.split("\n---\n", 1)
            progress_text = parts[0]
            # progress_bar 的 text 参数不支持 HTML，只放纯文本
            progress_bar.progress(min(0.05 + p * 0.15, 0.19), text=progress_text)
            status.info(f"📡 {progress_text}")
            # 实时滚动显示最新获取的视频标题
            if len(parts) > 1 and parts[1].strip():
                title_lines = parts[1].strip().split("\n")
                titles_html = "<br>".join(title_lines)
                live_detail.markdown(
                    f"<div style='background:#f8f9fa;border-left:3px solid #4CAF50;"
                    f"padding:8px 12px;border-radius:4px;font-size:13px;"
                    f"color:#555;max-height:150px;overflow-y:auto'>"
                    f"📋 最新视频:<br>{titles_html}</div>",
                    unsafe_allow_html=True,
                )
            elif p > 0:
                # 还没有标题时也显示动态提示
                live_detail.markdown(
                    f"<div style='background:#f8f9fa;border-left:3px solid #4CAF50;"
                    f"padding:8px 12px;border-radius:4px;font-size:13px;"
                    f"color:#888'>⏳ 正在获取视频数据，请稍候...</div>",
                    unsafe_allow_html=True,
                )

        videos = get_creator_videos(
            douyin_id,
            max_videos=max_videos,
            progress_callback=_on_fetch_progress,
            keyword=keyword,
        )
        live_detail.empty()

        # 从 f2 返回的数据中自动提取博主名称
        creator_name = ""
        for v in videos:
            name = v.get("creator_name") or v.get("author") or ""
            if name:
                creator_name = name
                break
        if not creator_name:
            creator_name = douyin_id

        # 显示结果（关键词过滤已在 f2_worker 中完成）
        if keyword:
            status.success(f"✅「{creator_name}」关键词「{keyword}」匹配 {len(videos)} 个视频")
            if not videos:
                progress_bar.empty()
                st.warning(f"没有找到标题包含「{keyword}」的视频")
                return
        else:
            status.success(f"✅ 获取到「{creator_name}」的 {len(videos)} 个视频")
        save_video_list(videos, douyin_id)

        # ─── 步骤1.5: 合并已有的转录结果（断点续传核心）───
        if resume:
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
                    status.info(f"🔄 从上次结果恢复了 {merged} 条已有转录")

        # ─── 步骤2: 下载音频 ───
        # 跳过已有转录的视频，只下载需要转录的
        need_audio = [v for v in videos
                      if not (v.get("transcript") and isinstance(v.get("transcript"), dict)
                              and v["transcript"].get("text"))
                      and v.get("audio_url")]
        if need_audio:
            status.info(f"🔊 下载 {len(need_audio)} 个视频的音频（跳过已转录的）...")
            progress_bar.progress(0.2, text="下载音频...")
            _dl_done_titles = []

            def _on_download_progress(p, t):
                progress_bar.progress(min(0.2 + p * 0.3, 0.49), text=t)
                status.info(f"🔊 {t}")
                # 提取当前下载的标题并展示最近的
                _dl_done_titles.append(t.split(": ", 1)[-1] if ": " in t else t)
                if len(_dl_done_titles) > 8:
                    _dl_done_titles.pop(0)
                live_detail.markdown(
                    f"<div style='background:#f8f9fa;border-left:3px solid #2196F3;"
                    f"padding:8px 12px;border-radius:4px;font-size:13px;"
                    f"color:#555;max-height:120px;overflow-y:auto'>"
                    f"🔊 最近下载:<br>{'<br>'.join('▸ ' + t for t in _dl_done_titles[-6:])}</div>",
                    unsafe_allow_html=True,
                )

            downloaded_videos = download_all_audios(
                need_audio,
                progress_callback=_on_download_progress,
            )
            live_detail.empty()

            # 把下载结果合并回 videos
            dl_map = {v["id"]: v for v in downloaded_videos if v.get("id")}
            for v in videos:
                vid = v.get("id")
                if vid and vid in dl_map:
                    v["audio_path"] = dl_map[vid].get("audio_path")
                    v["downloaded"] = dl_map[vid].get("downloaded", False)

            downloaded = sum(1 for v in downloaded_videos if v.get("downloaded"))
            status.success(f"✅ 成功下载 {downloaded}/{len(need_audio)} 个音频")
        else:
            if resume:
                status.success("✅ 所有视频已有转录，无需下载音频")
            else:
                status.warning("⚠️ 没有可用的音频链接")

        # ─── 步骤3: 并发转录（5 workers + 断点续传 + 重试）───
        # 需要转录的 = 有音频文件但还没有转录结果的
        need_transcribe_list = [
            v for v in videos
            if v.get("audio_path") and Path(v["audio_path"]).exists()
            and not (v.get("transcript") and isinstance(v.get("transcript"), dict) and v["transcript"].get("text"))
        ]
        already_done = sum(1 for v in videos if v.get("transcript") and isinstance(v.get("transcript"), dict) and v["transcript"].get("text"))
        need_transcribe = len(need_transcribe_list)

        if need_transcribe > 0:
            if already_done > 0:
                status.info(f"🤖 云端并发转录: 已有 {already_done} 个，剩余 {need_transcribe} 个...")
            else:
                status.info(f"🤖 OpenAI Whisper API 并发转录 {need_transcribe} 个音频（5路并发）...")
            progress_bar.progress(0.5, text="云端并发转录中...")

            import time as _time
            _transcribe_start = _time.time()

            _transcribed_titles = []

            def _on_progress(done, total, title):
                elapsed = int(_time.time() - _transcribe_start)
                elapsed_str = f"{elapsed // 60}分{elapsed % 60:02d}秒" if elapsed >= 60 else f"{elapsed}秒"
                remaining = ""
                if done > 0:
                    est_total = elapsed * total / done
                    est_remaining = int(est_total - elapsed)
                    if est_remaining > 60:
                        remaining = f"，预计还需 {est_remaining // 60}分{est_remaining % 60:02d}秒"
                    else:
                        remaining = f"，预计还需 {est_remaining}秒"
                progress_bar.progress(
                    min(0.5 + (done / max(total, 1)) * 0.35, 0.84),
                    text=f"并发转录中 ({done}/{total}): {title} | {elapsed_str}{remaining}"
                )
                status.info(f"🤖 转录 {done}/{total}: {title} | 已耗时 {elapsed_str}{remaining}")
                _transcribed_titles.append(f"✅ {title}")
                if len(_transcribed_titles) > 10:
                    _transcribed_titles.pop(0)
                live_detail.markdown(
                    f"<div style='background:#f8f9fa;border-left:3px solid #FF9800;"
                    f"padding:8px 12px;border-radius:4px;font-size:13px;"
                    f"color:#555;max-height:150px;overflow-y:auto'>"
                    f"🤖 已转录 {done}/{total}:<br>"
                    f"{'<br>'.join(_transcribed_titles[-8:])}</div>",
                    unsafe_allow_html=True,
                )

            videos = transcribe_batch(
                videos,
                progress_callback=_on_progress,
                save_callback=lambda: save_transcripts(videos, douyin_id),
            )
            live_detail.empty()

        # 对没有转录的视频，用视频描述作为 fallback
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

        status.success(
            f"✅ 云端转录 {whisper_count} 个"
            + (f"，视频描述补充 {desc_count} 个" if desc_count else "")
        )

        # ─── 步骤4: 生成 Word 文档 ───
        status.info("📄 生成 Word 文档...")
        progress_bar.progress(0.9, text="生成 Word 文档...")

        doc_path = generate_word_doc(videos, creator_name, douyin_id)

        # ─── 步骤5: 清理音频文件 + checkpoint，然后保存最终结果 ───
        _cleanup_audio_files(videos)
        clear_checkpoint(douyin_id)
        save_transcripts(videos, douyin_id)

        progress_bar.progress(1.0, text="✅ 完成！")

        status.success(
            f"🎉 完成！共 {len(videos)} 个视频，转录 {whisper_count} 个，"
            f"描述补充 {desc_count} 个"
        )

        # 保存到 session state
        st.session_state["current_transcripts"] = videos
        st.session_state["current_creator_id"] = douyin_id
        st.session_state["current_creator_name"] = creator_name
        st.session_state["doc_path"] = str(doc_path)

    except Exception as e:
        progress_bar.empty()
        status.error(f"❌ 出错了: {str(e)}")
        st.exception(e)


def _show_results():
    """显示提取结果"""
    videos = st.session_state["current_transcripts"]
    transcribed = [v for v in videos if v.get("transcript")]

    st.divider()
    st.subheader(f"📊 结果：{len(transcribed)}/{len(videos)} 个视频已转录")

    # Word 下载按钮
    if "doc_path" in st.session_state:
        doc_path = Path(st.session_state["doc_path"])
        if doc_path.exists():
            with open(doc_path, "rb") as f:
                st.download_button(
                    label="📥 下载 Word 文档",
                    data=f.read(),
                    file_name=doc_path.name,
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    type="primary",
                )

    # 文字稿预览
    with st.expander("📜 文字稿预览", expanded=False):
        for i, v in enumerate(transcribed[:20]):
            st.markdown(f"**{i+1}. {v.get('title', '无标题')[:60]}**")
            transcript = v.get("transcript", {})
            text = transcript.get("text", "") if isinstance(transcript, dict) else str(transcript)
            st.text(text[:300] + ("..." if len(text) > 300 else ""))
            st.divider()

        if len(transcribed) > 20:
            st.caption(f"...还有 {len(transcribed) - 20} 条文字稿，请下载 Word 查看完整内容")


def _render_chat_tab():
    """博主 GPT 对话界面"""

    if "current_transcripts" not in st.session_state:
        st.info("👈 请先在「提取文字稿」标签页中提取博主的视频文字稿")
        return

    if not OPENAI_API_KEY:
        st.error("❌ 需要设置 OPENAI_API_KEY 才能使用对话功能")
        return

    creator_id = st.session_state.get("current_creator_id", "")
    creator_name = st.session_state.get("current_creator_name", "博主")
    videos = st.session_state["current_transcripts"]

    st.subheader(f"💬 与「{creator_name}」对话")
    st.caption(f"基于 {sum(1 for v in videos if v.get('transcript'))} 个视频的文字稿，使用 GPT-4.1 模仿博主风格回复")

    # 对话引擎和消息都按博主 ID 隔离
    engine_key = f"chat_engine_{creator_id}"
    messages_key = f"chat_messages_{creator_id}"

    if engine_key not in st.session_state:
        try:
            st.session_state[engine_key] = CreatorChat(creator_name, videos)
        except ValueError as e:
            st.error(str(e))
            return

    chat_engine: CreatorChat = st.session_state[engine_key]

    # 重置按钮
    if st.button("🔄 重新开始对话"):
        chat_engine.reset()
        st.session_state[messages_key] = []
        st.rerun()

    # 对话历史显示
    if messages_key not in st.session_state:
        st.session_state[messages_key] = []

    for msg in st.session_state[messages_key]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # 用户输入
    if prompt := st.chat_input(f"问「{creator_name}」点什么..."):
        st.session_state[messages_key].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner(f"「{creator_name}」正在思考..."):
                response = chat_engine.chat(prompt)
            st.markdown(response)

        st.session_state[messages_key].append({"role": "assistant", "content": response})


if __name__ == "__main__":
    main()
