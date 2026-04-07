# 重建方案：本地 CLI 抖音文稿提取工具

## 背景

改了十几次都没修好，代码越改越乱。根因是两个：
1. **audio_url 是背景音乐**，不是口述音频 — 导致所有文稿相同
2. **video_play_url 会过期** — 必须下载前实时获取

现在从头来，用干净的 CLI 脚本替代 Streamlit/Vercel，在本地运行。

## 方案：新建 `run.py` CLI 入口

### 保留不动的模块（已验证可用）
- `f2_worker.py` — 视频列表翻页 + checkpoint
- `f2_detail_worker.py` — 实时获取视频 URL
- `doc_generator.py` — Word 文档生成
- `chat_engine.py` — GPT 对话
- `config.py` — 配置管理
- `scraper.py` — `_fetch_fresh_video_url()` + `download_video_audio()` + `resolve_douyin_input()`（已修复，不用背景音乐 fallback）

### 修改1：`transcriber.py` — 代理改为可选
- 第19行 `_PROXY` 改为从环境变量读取，空则不用代理
- `_make_client()` 只在有代理时传 proxy 参数

### 修改2：新建 `run.py` — CLI 入口（约150行）

```
用法: python run.py "抖音链接" [--keyword 关键词] [--max-videos 50] [--resume]

流程:
  Step 1: 获取视频列表（f2_worker）
  Step 2: 逐个下载音频（实时获取URL → 下载视频 → ffmpeg提取）
  Step 3: 并行转录（5路 Whisper）
  Step 4: 生成 Word 文档
  Step 5: 清理临时文件
```

关键设计：
- 下载是**逐个顺序执行**的（每个视频先获取新鲜URL再立即下载）
- 每次下载间隔 2 秒（防限流）
- 转录是**5路并发**的（Whisper API 不受抖音限流影响）
- `--resume` 恢复已有转录，只处理新视频
- 不用 `--resume` 则彻底清空重来

### 不删除但不使用的文件
- `app.py` — Streamlit 界面（保留备用）
- `api/index.py` — Vercel API（保留备用）

## 涉及的文件

| 文件 | 操作 | 改动量 |
|------|------|--------|
| `run.py` | 重写 | ~150行 |
| `transcriber.py` | 改2处 | ~5行 |
| 其他所有模块 | 不动 | 0 |

## 验证方法

1. 清空 `douyin_data/` 所有文件
2. 运行 `python run.py "抖音分享链接"`
3. 检查终端输出：每个视频应显示"获取实时链接"
4. 打开生成的 Word 文档：每个视频文稿内容应不同
5. 对比前5个视频的文稿前50字 — 不应重复
