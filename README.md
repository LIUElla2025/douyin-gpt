# 抖音博主 GPT

> 输入抖音号或分享链接，自动提取博主所有视频的口述文字稿，生成 Word 文档，还能与「博主 AI 分身」对话。

---

## 目录

- [功能介绍](#功能介绍)
- [在线访问](#在线访问)
- [项目结构](#项目结构)
- [技术架构](#技术架构)
- [环境变量](#环境变量)
- [本地开发](#本地开发)
- [部署到 Vercel](#部署到-vercel)
- [API 接口](#api-接口)
- [常见问题](#常见问题)

---

## 功能介绍

| 功能 | 说明 |
|------|------|
| **文字稿提取** | 自动获取博主所有视频，通过 OpenAI Whisper 将口述内容转为文字 |
| **Word 文档导出** | 一键生成包含全部文字稿的 `.docx` 文件，可直接下载 |
| **博主 GPT 对话** | 基于文字稿内容，模仿博主的思维方式和表达风格进行 AI 对话 |
| **关键词筛选** | 支持按关键词过滤视频，精准提取目标内容 |
| **实时进度** | SSE 流式推送，实时显示抓取和转录进度 |

---

## 在线访问

部署在 Vercel 上，打开即用，无需安装：

```
https://douyin-gpt.vercel.app
```

---

## 项目结构

```
douyin-gpt/
├── api/
│   └── index.py          # Flask 后端（Vercel Serverless Function）
├── public/
│   └── index.html         # 前端单页应用
├── vercel.json            # Vercel 部署配置
├── requirements.txt       # Python 依赖
└── README.md              # 本文档
```

---

## 技术架构

```
用户输入抖音号/链接
       │
       ▼
  resolve-url ── 解析链接，提取 sec_uid
       │
       ▼
  fetch-videos ── SSE 流式推送，逐页获取视频列表
       │
       ▼
   transcribe ── 下载视频文件 → OpenAI Whisper API 转录口述内容
       │
       ▼
  generate-doc ── 汇总文字稿，生成 Word 文档
       │
       ▼
     chat ── 基于文字稿，模仿博主风格进行 AI 对话
```

**核心技术栈：**

- **后端**：Flask + Vercel Python Serverless
- **前端**：原生 HTML/CSS/JS 单页应用
- **数据获取**：直接调用抖音 API（XBogus 签名算法）
- **语音转文字**：OpenAI Whisper API（云端转录）
- **AI 对话**：OpenAI GPT API
- **文档生成**：python-docx

---

## 环境变量

在 Vercel 项目设置中配置以下环境变量：

| 变量名 | 必需 | 说明 |
|--------|------|------|
| `OPENAI_API_KEY` | 是 | OpenAI API Key，用于 Whisper 转录和 GPT 对话 |
| `APIFY_API_TOKEN` | 否 | Apify API Token，备用数据源 |

---

## 本地开发

```bash
# 1. 克隆项目
git clone https://github.com/your-repo/douyin-gpt.git
cd douyin-gpt

# 2. 安装依赖
pip install -r requirements.txt

# 3. 设置环境变量
export OPENAI_API_KEY="sk-..."

# 4. 启动开发服务器
cd api && flask run --port 5000
```

打开浏览器访问 `http://localhost:5000`。

---

## 部署到 Vercel

```bash
# 安装 Vercel CLI
npm i -g vercel

# 登录并部署
vercel login
vercel --prod
```

部署前确保已在 Vercel 项目设置中配置好环境变量。

---

## API 接口

所有接口均为 `POST` 请求，请求体为 JSON 格式。

### `POST /api/resolve-url`

解析抖音分享链接，提取博主 `sec_uid`。

```json
{ "url": "https://v.douyin.com/xxx" }
```

### `POST /api/fetch-videos`

获取博主视频列表，返回 SSE 流式数据。

```json
{ "sec_uid": "MS4wLjAB...", "keyword": "可选关键词" }
```

### `POST /api/transcribe`

转录单个视频的口述内容。

```json
{
  "video_download_url": "视频下载地址",
  "audio_url": "备用音频地址",
  "video_url": "视频播放页地址"
}
```

### `POST /api/generate-doc`

根据文字稿生成 Word 文档，返回 `.docx` 文件。

```json
{ "videos": [{ "title": "标题", "transcript": "文字稿" }] }
```

### `POST /api/chat`

基于文字稿内容进行 AI 对话。

```json
{ "message": "用户消息", "context": "文字稿上下文" }
```

---

## 常见问题

**Q：转录结果是背景音乐而不是口述内容？**

已修复。当前版本下载视频文件（包含完整音轨）发送给 Whisper，而非仅提取背景音乐 URL。

**Q：转录失败，提示文件过大？**

Whisper API 文件上限为 25 MB。超过此限制的视频会自动跳过，不影响其他视频的转录。

**Q：抓取速度慢或超时？**

视频列表通过 SSE 流式推送，最多获取 200 条视频。Vercel Serverless 单次请求上限约 300 秒，系统在 250 秒时自动停止并返回已获取的数据。

---

*基于 Flask + Vercel 构建*
