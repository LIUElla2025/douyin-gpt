# 抖音博主 GPT

输入抖音号 → 自动提取所有视频文字稿 → 生成 Word 文档 + 博主 AI 对话

## 功能

- **文字稿提取** — 自动获取博主所有视频，提取文字稿（抖音字幕 / Whisper 语音转文字）
- **Word 文档** — 生成包含所有文字稿的 Word 文档，可下载
- **博主 GPT** — 基于文字稿模仿博主的思维方式和说话风格进行对话

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Keys
cp .env.example .env
# 编辑 .env 填入你的 API Keys

# 3. 启动
streamlit run app.py
```

浏览器自动打开 `http://localhost:8501`

## 配置

| 环境变量 | 必需 | 说明 |
|---------|------|------|
| `APIFY_API_TOKEN` | 是 | Apify API Token，用于抓取抖音视频 |
| `ANTHROPIC_API_KEY` | 对话功能需要 | Claude API Key，用于博主 GPT 对话 |
| `WHISPER_MODEL` | 否 | Whisper 模型大小，默认 `base` |

## 技术栈

- **数据获取**: Apify 抖音 Actor → douyin-tiktok-scraper → yt-dlp（三层 fallback）
- **语音转文字**: OpenAI Whisper（本地运行）
- **AI 对话**: Claude API（模仿博主风格）
- **文档生成**: python-docx
- **Web 界面**: Streamlit
