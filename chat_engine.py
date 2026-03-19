"""博主 GPT 对话引擎 - 使用 OpenAI GPT-4.1（1M 上下文）"""

import json
import re
import httpx
from config import OPENAI_API_KEY, DATA_DIR

_PROXY = "http://127.0.0.1:7890"

_openai = None

# 对话历史持久化目录
_HISTORY_DIR = DATA_DIR / "chat_history"
_HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _get_openai():
    """延迟 import openai"""
    global _openai
    if _openai is None:
        try:
            import openai
            _openai = openai
        except ImportError:
            raise ImportError(
                "openai 未安装。请运行: pip install openai"
            )
    return _openai

# GPT-4.1 有 1M token 上下文，可以装下所有文稿
# 中文字符 ≈ 1.5 tokens，500K 字符 ≈ 750K tokens，留 250K 给对话
_MAX_PROFILE_CHARS = 500000


def _is_garbage_transcript(text: str) -> bool:
    """检测垃圾文稿：背景音乐歌词、水印、广告语、重复噪音等非口播内容"""
    if not text or len(text.strip()) < 10:
        return True

    garbage_phrases = [
        "YoYo Television Series Exclusive", "优优独播剧场",
        "请不吝点赞", "订阅、转发、打赏", "点点栏目", "Television Series",
    ]
    if sum(1 for p in garbage_phrases if p in text) >= 2:
        return True

    clean = text.strip()

    # 检测高重复率：任意2-6字短语重复超过5次（水印/ASR噪音特征）
    if len(clean) > 20:
        if clean.count(clean[:20]) >= 3:
            return True
        # 检查任意短语的高频重复（如"王努钦"重复20次）
        from collections import Counter
        words = re.findall(r'[\u4e00-\u9fff]{2,6}', clean)
        if words:
            freq = Counter(words)
            most_common_word, most_common_count = freq.most_common(1)[0]
            if most_common_count >= 5 and (most_common_count * len(most_common_word)) > len(clean) * 0.3:
                return True

    # 检测歌曲/音乐元数据（作词、作曲、编曲、演唱等）
    music_keywords = ["作词", "作曲", "编曲", "演唱", "混音", "和声", "制作人", "词曲", "原创"]
    music_hits = sum(1 for k in music_keywords if k in clean)
    if music_hits >= 3:
        return True

    # 检测歌曲名描述（只有一句话提到歌名）
    if len(clean) < 50 and ("歌词叫做" in clean or "歌曲叫做" in clean or "这首歌" in clean):
        return True

    # 检测影视剧对白（大量破折号对话）
    if clean.count("——") >= 4 and len(clean) > 50:
        return True

    # 检测极短文稿（不足50字的大概率不是有效口述）
    if len(clean) < 50:
        return True

    # 检测纯数字/标点内容（ASR把背景音识别成数字序列）
    non_num = re.sub(r'[\d\s，、,.\n]+', '', clean)
    if len(non_num) < len(clean) * 0.2:
        return True

    # 检测内容占比（去掉垃圾短语后实质内容不足20%）
    filtered = text
    for p in garbage_phrases:
        filtered = filtered.replace(p, "")
    filtered_clean = re.sub(r'[\s，。？！、；：,.\?!;:\n]+', '', filtered)
    original_clean = re.sub(r'[\s，。？！、；：,.\?!;:\n]+', '', text)
    if original_clean and len(filtered_clean) < len(original_clean) * 0.2:
        return True

    return False


def build_creator_profile(videos: list[dict]) -> str:
    """从文字稿中构建博主内容样本，控制总长度（自动过滤垃圾文稿）"""
    all_texts = []
    skipped = 0
    for v in videos:
        t = v.get("transcript")
        if t:
            text = t.get("text", "") if isinstance(t, dict) else str(t)
            if text:
                if _is_garbage_transcript(text):
                    skipped += 1
                    continue
                title = v.get("title", "")
                all_texts.append(f"【{title}】\n{text}")
    if skipped:
        print(f"  已过滤 {skipped} 条垃圾文稿（背景音乐/水印/广告）")

    if not all_texts:
        return "该博主暂无可用的文字稿内容。"

    # 拼接文字稿，控制总长度不超过预算
    parts = []
    total_len = 0
    for text in all_texts:
        if total_len + len(text) > _MAX_PROFILE_CHARS:
            remaining = _MAX_PROFILE_CHARS - total_len
            if remaining > 200:
                parts.append(text[:remaining] + "...")
            break
        parts.append(text)
        total_len += len(text)

    return "\n\n---\n\n".join(parts)


def build_system_prompt(creator_name: str, creator_profile: str) -> str:
    """构建模仿博主风格的 system prompt（全量文稿，无需检索）"""
    return f"""你是抖音博主「{creator_name}」的 AI 分身。你的任务是完全模仿这位博主的思维方式、说话风格、用词习惯和知识领域来回答问题。

## 你的身份
- 你就是「{creator_name}」，用第一人称说话
- 模仿这位博主的语气、口头禅、表达方式
- 回答问题时基于博主在视频中表达过的观点和知识
- 如果被问到博主没有涉及过的话题，用博主的风格说"这个我还真没怎么聊过"之类的话

## 博主的全部视频内容（用于学习风格和知识）
以下是博主所有视频的完整文字稿，请充分利用这些内容来理解博主的观点、知识体系和表达方式：

{creator_profile}

## 注意：文稿噪音过滤
视频文字稿由语音识别生成，可能混入以下非口播内容，请自动忽略：
- 背景音乐歌词（如歌曲片段、古诗词朗诵）
- 视频水印文字（如"优优独播剧场"、"YoYo Television Series Exclusive"）
- 平台引导语（如"请不吝点赞、订阅、转发、打赏"）
- 出品方信息（如"由XX剧团出品"）
- 与视频标题主题明显无关的内容（如视频讲情感但文稿是菜谱）
只使用博主真正口播的观点和内容来回答。

## 行为规则
1. 始终保持博主的说话风格和语气
2. 优先使用博主在视频中表达过的观点来回答
3. 不要说"根据视频内容"或"在某个视频中"这样的元叙述，直接用博主的口吻说
4. 回答要自然、口语化，像博主在和粉丝聊天
5. 如果不确定博主的观点，可以用博主的风格表达自己的推测，但要坦诚
6. 用户之前在对话中提供的背景信息、个人情况等，请始终记住并在后续回答中参考"""


class CreatorChat:
    """博主 GPT 对话管理器 — 使用 OpenAI GPT-4.1（1M 上下文）"""

    def __init__(self, creator_name: str, videos: list[dict]):
        if not OPENAI_API_KEY:
            raise ValueError("请设置 OPENAI_API_KEY 环境变量")

        openai = _get_openai()
        self.client = openai.OpenAI(
            api_key=OPENAI_API_KEY,
            http_client=httpx.Client(proxy=_PROXY, timeout=120),
        )
        self.creator_name = creator_name
        self.creator_profile = build_creator_profile(videos)
        self.history: list[dict] = []
        self._history_path = _HISTORY_DIR / f"{re.sub('[^a-zA-Z0-9_\u4e00-\u9fff]', '_', creator_name)}.json"
        self._load_history()

    def _load_history(self):
        """从文件恢复对话历史"""
        if self._history_path.exists():
            try:
                with open(self._history_path, "r", encoding="utf-8") as f:
                    self.history = json.load(f)
                print(f"  已恢复 {len(self.history)} 条对话历史")
            except Exception:
                self.history = []

    def _save_history(self):
        """持久化对话历史到文件"""
        try:
            with open(self._history_path, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def chat(self, user_message: str) -> str:
        """与博主 GPT 对话 — 全量文稿 + 完整历史"""
        # 构建 system prompt（全量文稿已包含，无需检索）
        system = build_system_prompt(self.creator_name, self.creator_profile)

        # 添加用户消息到历史
        self.history.append({"role": "user", "content": user_message})

        # GPT-4.1 有 1M token 上下文，保留最近 200 条消息（约100轮对话）
        recent_history = self.history[-200:]

        # 构建消息列表
        messages = [{"role": "system", "content": system}] + recent_history

        try:
            response = self.client.chat.completions.create(
                model="gpt-4.1",
                max_tokens=16384,
                messages=messages,
            )

            if not response.choices:
                self.history.pop()
                return "对话出错: 模型返回了空响应"

            assistant_message = response.choices[0].message.content
            self.history.append({"role": "assistant", "content": assistant_message})
            self._save_history()
            return assistant_message

        except Exception as e:
            self.history.pop()
            return f"对话出错: {str(e)}"

    def reset(self):
        """清空对话历史"""
        self.history = []
        self._save_history()
