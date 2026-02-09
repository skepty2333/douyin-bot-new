"""
AI 总结模块 — 三阶段管线

Stage 1  Gemini           音频转写 + 初稿总结
Stage 2  DeepSeek Reasoner 对初稿进行深度审视、提出补充问题
Stage 3  Sonnet 4.5        联网搜索补充信息, 输出最终版 Markdown

本模块只负责生成 Markdown 文本, PDF 渲染由 pdf_generator.py 负责。
"""
import base64
import os
import logging
import httpx
from config import (
    API_BASE_URL,
    GEMINI_API_KEY, GEMINI_MODEL,
    DEEPSEEK_API_KEY, DEEPSEEK_MODEL,
    SONNET_API_KEY, SONNET_MODEL,
)

logger = logging.getLogger(__name__)

# ============================================================
# 公共 API 调用
# ============================================================

async def _chat(
    model: str,
    messages: list,
    api_key: str,
    max_tokens: int = 8192,
    temperature: float = 0.3,
    timeout: int = 180,
) -> str:
    """统一的 OpenAI 兼容 chat/completions 调用"""
    url = f"{API_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    return data["choices"][0]["message"]["content"]


# ============================================================
# Stage 1 — Gemini: 转写 + 初稿
# ============================================================

STAGE1_SYSTEM = """你是一个专业的视频内容转写与总结助手。

请完成两件事：
1. 完整转写音频中的所有口述内容（不要遗漏任何观点、数据、案例）
2. 基于转写内容，输出一份结构化的 Markdown 学习笔记

## 输出格式

# 视频标题

> 核心摘要：一句话概括

## 核心要点
1. **要点一**：说明
2. **要点二**：说明
...

## 详细笔记

### 小节标题
- 具体内容...
- 关键数据/案例...

（按视频逻辑分多个小节）

## 关键收获
1. ...
2. ...

## 原始转写文本
> 在此处放置完整的逐字转写内容，用引用块包裹。

---

## 规范
- **加粗**星号紧贴文字
- 列表项 `- ` 或 `1. ` 后必须有空格
- 标题 `#` 后必须有空格
- 使用中文，保留专业术语并给出解释
- 尽可能保留视频中提到的所有具体数字、人名、书名、案例
"""


async def stage1_transcribe_and_draft(
    audio_path: str,
    video_title: str = "",
    video_author: str = "",
    user_requirement: str = "",
) -> str:
    """Gemini 多模态: 音频 → 转写+初稿 Markdown"""
    logger.info("[Stage1] Gemini 转写+初稿")

    file_size = os.path.getsize(audio_path)
    if file_size > 24 * 1024 * 1024:
        return await _stage1_large_audio(audio_path, video_title, video_author, user_requirement)

    with open(audio_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode()

    user_parts = _build_context(video_title, video_author, user_requirement)

    messages = [
        {"role": "system", "content": STAGE1_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "mp3"}},
                {"type": "text", "text": user_parts},
            ],
        },
    ]

    try:
        result = await _chat(GEMINI_MODEL, messages, GEMINI_API_KEY, timeout=240)
        logger.info(f"[Stage1] 完成, 长度={len(result)}")
        return result
    except Exception as e:
        logger.warning(f"[Stage1] 多模态失败, 回退转写+总结: {e}")
        return await _stage1_fallback(audio_path, video_title, video_author, user_requirement)


async def _stage1_fallback(audio_path, title, author, req) -> str:
    """回退: whisper 转写 → 文本总结"""
    transcript = await _transcribe_audio(audio_path)
    prompt = _build_context(title, author, req)
    prompt += f"\n\n以下是视频的完整转写文本:\n\n{transcript}"
    messages = [
        {"role": "system", "content": STAGE1_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    return await _chat(GEMINI_MODEL, messages, GEMINI_API_KEY)


async def _stage1_large_audio(audio_path, title, author, req) -> str:
    """大音频: 分段转写 → 合并 → 总结"""
    import subprocess
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True, timeout=10,
    )
    duration = float(probe.stdout.strip())

    segments, start, i = [], 0, 0
    while start < duration:
        seg = audio_path.replace(".mp3", f"_seg{i}.mp3")
        subprocess.run(
            ["ffmpeg", "-ss", str(start), "-i", audio_path, "-t", "600",
             "-acodec", "libmp3lame", "-ab", "128k", "-y", seg],
            capture_output=True, timeout=60,
        )
        if os.path.exists(seg):
            segments.append(seg)
        start += 600
        i += 1

    parts = []
    for seg in segments:
        try:
            parts.append(await _transcribe_audio(seg))
        except Exception as e:
            logger.warning(f"分段转写失败: {e}")
        finally:
            try:
                os.remove(seg)
            except OSError:
                pass

    transcript = "\n".join(parts)
    prompt = _build_context(title, author, req)
    prompt += f"\n\n以下是视频的完整转写文本:\n\n{transcript}"
    messages = [
        {"role": "system", "content": STAGE1_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    return await _chat(GEMINI_MODEL, messages, GEMINI_API_KEY)


async def _transcribe_audio(audio_path: str) -> str:
    """whisper API 转写"""
    url = f"{API_BASE_URL}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GEMINI_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            with open(audio_path, "rb") as f:
                resp = await client.post(
                    url, headers=headers,
                    files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
                    data={"model": "whisper-1", "language": "zh", "response_format": "text"},
                )
                resp.raise_for_status()
                return resp.text
    except Exception as e:
        logger.warning(f"whisper 失败, 用 Gemini 兜底: {e}")
        with open(audio_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        messages = [
            {"role": "system", "content": "完整转写音频为中文文本，只输出转写内容。"},
            {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}},
                {"type": "text", "text": "请转写。"},
            ]},
        ]
        return await _chat(GEMINI_MODEL, messages, GEMINI_API_KEY, temperature=0.1)


# ============================================================
# Stage 2 — DeepSeek Reasoner: 深度审视
# ============================================================

STAGE2_SYSTEM = """你是一位博学严谨的知识审计专家。你将收到一份 AI 根据视频音频生成的学习笔记初稿。

你的任务是对这份初稿进行 **深度审视**，提出有价值的补充问题和改进建议，帮助后续 AI 将这份笔记从"浅层概括"提升为"深度学习资料"。

## 你必须完成以下分析

### 一、内容缺失审查
逐一检查笔记中提到但未展开的：
- 专业术语 / 概念：是否给出了准确定义？是否需要补充背景知识？
- 人名 / 机构：是否需要补充此人/机构的背景介绍？
- 数据 / 统计：是否缺乏来源或对比数据？
- 案例 / 事件：是否需要更多上下文？
- 方法论 / 框架：是否给出了可操作的步骤？

### 二、深度不足诊断
- 笔记中哪些论点只有"结论"没有"论证过程"？
- 哪些建议太笼统，需要量化或具体化？
- 是否存在值得对比讨论的反面观点或争议？

### 三、知识拓展建议
- 视频主题关联的重要概念/理论，笔记中完全没有提及但学习者应该知道的
- 推荐的延伸阅读方向

## 输出格式

严格使用以下 Markdown 格式输出：

# 审查报告

## 需要补充解释的概念
1. **[概念名称]** — 为什么需要补充 + 建议搜索的关键词
2. ...

## 需要补充的背景信息
1. **[人名/机构/事件]** — 需要补充什么 + 建议搜索关键词
2. ...

## 需要深化的论点
1. **[论点]** — 当前问题 + 如何改进
2. ...

## 建议补充的关联知识
1. **[知识点]** — 与视频主题的关联 + 搜索关键词
2. ...

## 具体搜索任务清单
按优先级列出 5-10 个最值得搜索补充的条目：
1. 搜索: "[具体搜索关键词]" — 用于补充 [什么内容]
2. ...

注意：
- 每条建议都必须附带 **具体的搜索关键词**，方便下游 AI 直接执行搜索
- 优先关注视频核心主题相关的问题，不要纠结边缘细节
- 如果初稿质量已经很高某方面无需补充，请直接说"此方面无需补充"
"""


async def stage2_critical_review(draft_markdown: str) -> str:
    """DeepSeek Reasoner 对初稿进行深度审视"""
    logger.info("[Stage2] DeepSeek 深度审视")

    messages = [
        {"role": "system", "content": STAGE2_SYSTEM},
        {
            "role": "user",
            "content": (
                "以下是 AI 根据一个知识视频生成的学习笔记初稿，请进行深度审视：\n\n"
                "---\n\n"
                f"{draft_markdown}\n\n"
                "---\n\n"
                "请按照要求输出审查报告。"
            ),
        },
    ]

    result = await _chat(DEEPSEEK_MODEL, messages, DEEPSEEK_API_KEY, max_tokens=4096, temperature=0.2, timeout=300)
    logger.info(f"[Stage2] 完成, 长度={len(result)}")
    return result


# ============================================================
# Stage 3 — Sonnet 4.5: 联网搜索 + 最终版
# ============================================================

STAGE3_SYSTEM = """你是一位顶级知识编辑。你的任务是将初稿重写为一份 **完整、深入、样式精美** 的最终版视频笔记。

## 核心原则

1. **结构第一**：直接输出笔记内容，**严禁**输出"根据您的要求..."、"执行搜索任务..."等任何元说明。
2. **样式规范**：
   - **正文**：使用标准段落，**严禁**使用引用块（`> `）包裹正文（这会导致蓝色框）。
   - **重点/补充**：仅在"知识补充"或"核心摘要"处使用引用块。
   - **数学公式**：支持 LaTeX 格式
     - 行内公式：使用 `$公式$`（如 `$E=mc^2$`）
     - 块级公式：使用 `$$公式$$` 或代码块 ` ```math `
     - **重要禁止**：若公式中包含**中文**字符，**严禁**使用 LaTeX 格式（因为 PDF 渲染器不支持 LaTeX 中文）。请直接使用普通文本输出该公式。
   - **列表**：使用 `- ` 开头（短横线后必须有空格），不要用 `* `
3. **内容深度**：解释所有专业名词，补充背景知识。
4. **作者信息**：从初稿中提取视频作者名称。如果初稿未提供作者信息，则完全省略"视频作者"这一行，不要写占位符。

## 输出结构（严格遵守）

# [视频标题]

> **核心摘要**：[1-2句话]
> **视频作者**：[从初稿提取的作者名，如无则删除此行]

## 1. [小节标题]

[正文段落，不要用引用块...]

- [列表项1]
- [列表项2]

## 2. [小节标题]

[正文段落...]

```math
[复杂公式放入代码块]
```

> 💡 **知识补充**：[补充信息]

...

## 关键收获
1. ...
2. ...

## 延伸阅读
- ...

<!-- 搜索关键词清单（放在最后，不影响阅读） -->
## 附：搜索关键词
- ...
"""


async def stage3_enrich_and_finalize(
    draft_markdown: str,
    review_report: str,
    user_requirement: str = "",
) -> str:
    """Sonnet 4.5 联网搜索补充信息, 生成最终版"""
    logger.info("[Stage3] Sonnet 联网搜索 + 最终版")

    user_content = (
        "## 初稿\n\n"
        f"{draft_markdown}\n\n"
        "---\n\n"
        "## 审查报告\n\n"
        f"{review_report}\n\n"
        "---\n\n"
    )
    if user_requirement:
        user_content += f"## 用户特别要求\n\n{user_requirement}\n\n---\n\n"

    user_content += (
        "请按照以下步骤处理：\n"
        "1. 阅读审查报告中的搜索任务清单\n"
        "2. 对每个重要条目执行 web_search 搜索\n"
        "3. 将搜索结果融入初稿\n"
        "4. 输出最终版笔记（**直接以标题开始，不要有任何开场白**）\n\n"
        "开始吧。"
    )

    messages = [
        {"role": "system", "content": STAGE3_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    # Sonnet with web_search tool
    url = f"{API_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {SONNET_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": SONNET_MODEL,
        "messages": messages,
        "max_tokens": 12000,
        "temperature": 0.3,
        "tools": [
            {
                "type": "web_search_20250305",
                "name": "web_search",
            }
        ],
    }

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    # 提取文本内容 (可能混有 tool_use blocks)
    content_blocks = data["choices"][0]["message"].get("content", "")
    if isinstance(content_blocks, list):
        result = "\n".join(
            block.get("text", "") for block in content_blocks if block.get("type") == "text"
        )
    else:
        result = content_blocks

    logger.info(f"[Stage3] 完成, 长度={len(result)}")
    return result


# ============================================================
# 主入口
# ============================================================

async def summarize_with_audio(
    audio_path: str,
    video_title: str = "",
    video_author: str = "",
    user_requirement: str = "",
    progress_callback=None,
) -> str:
    """
    三阶段 AI 管线, 返回最终版 Markdown

    progress_callback: async callable(str) 用于向用户推送进度
    """
    async def notify(msg: str):
        if progress_callback:
            try:
                await progress_callback(msg)
            except Exception:
                pass

    # Stage 1
    await notify("🔬 [1/3] Gemini 正在转写音频并生成初稿...")
    draft = await stage1_transcribe_and_draft(
        audio_path, video_title, video_author, user_requirement,
    )
    await notify("✅ [1/3] 初稿生成完成")

    # Stage 2
    await notify("🧠 [2/3] DeepSeek 正在深度审视初稿...")
    review = await stage2_critical_review(draft)
    await notify("✅ [2/3] 审查报告生成完成")

    # Stage 3
    await notify("🌐 [3/3] Sonnet 正在联网搜索并生成最终版...")
    final = await stage3_enrich_and_finalize(draft, review, user_requirement)
    await notify("✅ [3/3] 最终版笔记生成完成")

    return final


# ============================================================
# 辅助
# ============================================================

def _build_context(title: str, author: str, requirement: str) -> str:
    parts = ["请对以下视频内容进行转写和总结："]
    if title:
        parts.append(f"视频标题：{title}")
    if author:
        parts.append(f"作者：{author}")
    if requirement:
        parts.append(f"\n用户的特别要求：{requirement}")
    else:
        parts.append("\n请按照默认格式进行全面总结。")
    return "\n".join(parts)
