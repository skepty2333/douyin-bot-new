"""
抖音视频知识总结 Bot - 主服务

交互设计:
  1. 用户发送抖音链接 → Bot 立即回复 "收到，..."
  2. 2分钟内用户可再发一条消息补充总结要求
  3. 超时或收到要求后 → 开始处理 (API解析 → 下载 → 音频 → AI总结)
  4. 处理完成 → 回复视频信息+视频码，最后回复PDF

技术要点:
  - 企业微信5秒回调超时 → 异步处理, 立即返回 "success"
  - 消息去重 (企业微信可能重试)
  - 用户会话状态管理 (等待要求阶段)
  - 核心解析逻辑: 模拟移动端 Requests 请求 + JSON 解析 (无需 Playwright)
"""
import asyncio
import logging
import os
import time
import random
import string
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional, Dict

from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse

from config import (
    CORP_ID, CALLBACK_TOKEN, CALLBACK_AES_KEY,
    TEMP_DIR, LOG_LEVEL, AGENT_ID,
)
from wechat_crypto import WXBizMsgCrypt
from wechat_api import send_text_message, send_markdown_message, upload_temp_media, get_access_token
from douyin_parser import (
    extract_url_from_text, extract_user_requirement,
    resolve_and_download, extract_audio, cleanup_files,
)
from ai_summarizer import summarize_with_audio
from pdf_generator import generate_pdf
from knowledge_store import KnowledgeStore, KnowledgeEntry, extract_tags_from_markdown

knowledge_db = KnowledgeStore()

# ========================
# 日志
# ========================
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("douyin-bot")

# ========================
# 初始化
# ========================
crypto = WXBizMsgCrypt(CALLBACK_TOKEN, CALLBACK_AES_KEY, CORP_ID)

# 消息去重
_processed_msgs: Dict[str, float] = {}
MSG_DEDUP_TTL = 300

def generate_video_code() -> str:
    """生成5位随机视频码 (小写字母+数字)"""
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choices(chars, k=5))

# ========================
# 用户会话状态管理
# ========================
WAIT_SECONDS = 120  # 等待要求的时间(秒)


@dataclass
class PendingTask:
    """一个待处理的视频任务"""
    user_id: str
    share_url: str
    share_text: str           # 原始分享文案中可能已有的要求
    extra_requirement: str = ""  # 用户后续补充的要求
    created_at: float = field(default_factory=time.time)
    timer_task: Optional[asyncio.Task] = None
    processing: bool = False


# user_id → PendingTask
_pending: Dict[str, PendingTask] = {}


# ========================
# FastAPI
# ========================

@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(TEMP_DIR, exist_ok=True)
    logger.info("🚀 抖音视频总结 Bot 启动")
    yield
    logger.info("Bot 关闭")


app = FastAPI(title="抖音视频总结Bot", lifespan=lifespan)


# ========================
# 企业微信回调
# ========================

@app.get("/callback")
async def verify_callback(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
):
    """GET - 验证URL有效性"""
    try:
        echo = crypto.verify_url(msg_signature, timestamp, nonce, echostr)
        logger.info("URL验证成功")
        return PlainTextResponse(content=echo)
    except Exception as e:
        logger.error(f"URL验证失败: {e}")
        return PlainTextResponse(content="error", status_code=403)


@app.post("/callback")
async def receive_message(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
):
    """POST - 接收消息"""
    body = (await request.body()).decode("utf-8")

    try:
        xml_text = crypto.decrypt_msg(body, msg_signature, timestamp, nonce)
        xml_root = ET.fromstring(xml_text)
        msg_type = xml_root.find("MsgType").text
        from_user = xml_root.find("FromUserName").text

        # 去重
        msg_id = (xml_root.find("MsgId").text or "") if xml_root.find("MsgId") is not None else ""
        create_time = (xml_root.find("CreateTime").text or "") if xml_root.find("CreateTime") is not None else ""
        dedup_key = f"{msg_id}_{create_time}"
        now = time.time()
        if dedup_key in _processed_msgs and now - _processed_msgs[dedup_key] < MSG_DEDUP_TTL:
            return PlainTextResponse(content="success")
        _processed_msgs[dedup_key] = now
        # 清理过期
        for k in [k for k, v in _processed_msgs.items() if now - v > MSG_DEDUP_TTL]:
            del _processed_msgs[k]

        if msg_type == "text":
            content = xml_root.find("Content").text or ""
            logger.info(f"📩 {from_user}: {content[:80]}")
            # 异步处理, 不阻塞回调
            asyncio.create_task(handle_message(from_user, content))
        else:
            logger.info(f"忽略消息类型: {msg_type}")

    except Exception as e:
        logger.error(f"处理回调异常: {e}", exc_info=True)

    return PlainTextResponse(content="success")


# ========================
# 消息路由与会话管理
# ========================

async def handle_message(user_id: str, content: str):
    """
    消息路由:
    - 如果用户处于等待要求状态 → 当作补充要求
    - 如果消息中有抖音链接 → 创建新任务, 进入等待
    - 否则 → 回复使用说明
    """
    try:
        # ---- 情况1: 用户正在等待状态, 这条消息是补充要求 ----
        if user_id in _pending and not _pending[user_id].processing:
            pending = _pending[user_id]

            # 检查这条消息是否又是一个新链接
            new_url = extract_url_from_text(content)
            if new_url:
                # 用户发了新链接, 取消旧任务, 重新开始
                if pending.timer_task and not pending.timer_task.done():
                    pending.timer_task.cancel()
                del _pending[user_id]
                # 按新链接重新处理
                await _start_new_task(user_id, content, new_url)
                return

            # 这条消息是补充要求
            pending.extra_requirement = content.strip()
            logger.info(f"📝 {user_id} 补充要求: {content[:60]}")

            # 取消定时器, 立即开始处理
            if pending.timer_task and not pending.timer_task.done():
                pending.timer_task.cancel()


            await _process_task(user_id)
            return

        # ---- 情况2: 新消息, 检查是否有抖音链接 ----
        url = extract_url_from_text(content)
        if url:
            await _start_new_task(user_id, content, url)
            return

        # ---- 情况3: 既不是补充要求, 也没有链接 ----
        # 检查是否有正在处理的任务
        if user_id in _pending and _pending[user_id].processing:
            await send_text_message(user_id, "视频正在处理中，请稍候...")
            return

        await send_text_message(
            user_id,
            "收到，发送抖音视频分享链接给我，我帮你总结视频内容。\n\n"
            "使用方式:\n"
            "1. 发送抖音分享链接 (直接从抖音复制粘贴即可)\n"
            "2. 我会回复“收到”，你可以在2分钟内追加总结要求\n"
            "3. 无答复两分钟后将按默认处理\n\n"
            "示例追加要求:\n"
            "• \"请重点关注投资策略部分\"\n"
            "• \"用表格对比文中几种方法\"\n"
            "• \"只需要给出核心结论\""
        )

    except Exception as e:
        logger.error(f"handle_message 异常: {e}", exc_info=True)
        try:
            await send_text_message(user_id, f"❌ 处理出错: {e}")
        except Exception:
            pass


async def _start_new_task(user_id: str, content: str, url: str):
    """创建新的视频处理任务, 进入等待要求阶段"""
    # 从分享文案中提取可能已有的要求
    inline_req = extract_user_requirement(content, url)

    task = PendingTask(
        user_id=user_id,
        share_url=url,
        share_text=inline_req,
    )
    _pending[user_id] = task

    # 回复用户
    # 使用统一回复文案
    msg = "收到，发送“开始”以立即处理或输入要求，无答复两分钟后将按默认处理。"
    await send_text_message(user_id, msg)

    # 启动定时器
    task.timer_task = asyncio.create_task(_wait_then_process(user_id))


async def _wait_then_process(user_id: str):
    """等待指定时间, 如果期间没有收到要求则自动处理"""
    try:
        await asyncio.sleep(WAIT_SECONDS)
        # 超时, 检查任务是否还在
        if user_id in _pending and not _pending[user_id].processing:
            logger.info(f"⏰ {user_id} 等待超时, 开始默认处理")
            # 超时不发消息，直接处理
            await _process_task(user_id)
    except asyncio.CancelledError:
        # 被取消说明用户已补充要求或发了新链接, 正常情况
        pass


async def _process_task(user_id: str):
    """执行视频处理的主流程"""
    if user_id not in _pending:
        return

    task = _pending[user_id]
    task.processing = True
    video_id = None

    try:
        # 合并要求: 分享文案中的 + 用户补充的
        requirement = task.share_text
        if task.extra_requirement:
            if task.extra_requirement.strip() in ("开始", "start", "ok", "好"):
                pass  # "开始" 是触发词, 不作为要求
            else:
                requirement = task.extra_requirement  # 显式要求优先

        # 1. Playwright 解析 + 下载
        # await send_text_message(user_id, "🔍 正在解析视频链接...")
        video_info = await resolve_and_download(task.share_url)
        video_id = video_info["video_id"]
        title = video_info["title"] or "未知标题"
        author = video_info["author"] or "未知作者"

        # await send_text_message(
        #     user_id,
        #     f"🎬 视频: {title}\n"
        #     f"👤 作者: {author}\n\n"
        #     f"⏳ 正在提取音频..."
        # )

        # 2. 提取音频
        audio_path = extract_audio(video_info["video_path"])
        
        # 生成视频码
        video_code = generate_video_code()
        
        # 发送确认信息
        await send_text_message(
            user_id,
            f"视频: {title}\n"
            f"作者: {author}\n"
            f"视频码: {video_code}\n\n"
            f"请耐心等待..."
        )

        # 3. 三阶段 AI 管线 (带进度回调)
        async def progress(msg: str):
            # 不发送中间进度消息
            pass

        summary = await summarize_with_audio(
            audio_path=audio_path,
            video_title=title,
            video_author=author,
            user_requirement=requirement,
            progress_callback=progress,
        )

        # 保存 Markdown 用于调试/重生成
        # 保存到永久目录 (项目下的 summaries/)
        # summaries_dir = os.path.join(os.path.dirname(__file__), "summaries")
        # os.makedirs(summaries_dir, exist_ok=True)
        # persistent_md_path = os.path.join(summaries_dir, f"{video_id}_summary.md")
        # try:
        #     with open(persistent_md_path, "w", encoding="utf-8") as f:
        #         f.write(summary)
        #     logger.info(f"Markdown已保存(永久): {persistent_md_path}")
        # except Exception as e:
        #     logger.error(f"保存 Markdown 失败(永久): {e}")
        
        # 同时保存到临时目录 (PDF 生成使用)
        # md_path = os.path.join(TEMP_DIR, f"{video_id}_summary.md")
        # try:
        #     with open(md_path, "w", encoding="utf-8") as f:
        #         f.write(summary)
        #     logger.info(f"Markdown已保存(临时): {md_path}")
        # except Exception as e:
        #     logger.error(f"保存 Markdown 失败(临时): {e}")

        # 存入知识库
        try:
            tags = extract_tags_from_markdown(summary)
            entry = KnowledgeEntry(
                video_id=video_id,
                title=title,
                author=author,
                source_url=task.share_url,
                summary_markdown=summary,
                tags=tags,
                user_requirement=requirement,
                video_code=video_code,
            )
            knowledge_db.save(entry)
            logger.info(f"已存入知识库: {title} [{video_code}]")
        except Exception as e:
            logger.error(f"❌ 知识库存储失败: {e}")

        # 4. 生成并发送 PDF 结果
        pdf_path = os.path.join(TEMP_DIR, f"{video_id}_summary.pdf")
        pdf_success = False
        
        try:
            # await send_text_message(user_id, "正在生成 PDF 报告...")
            pass
            
            if generate_pdf(summary, pdf_path):
                # 上传临时素材
                media_id = await upload_temp_media(pdf_path, "file")
                
                # 发送文件消息
                await _send_file_message(user_id, media_id)
                pdf_success = True
                logger.info(f"PDF发送成功: {pdf_path}")
            else:
                logger.warning("PDF生成失败，回退到文本模式")
                
        except Exception as e:
            logger.error(f"PDF处理流程异常: {e}")
            
        # 如果PDF失败，回退到 Markdown 消息
        if not pdf_success:
            await send_text_message(user_id, "PDF生成失败，发送文本内容：")
            await send_markdown_message(user_id, summary)

        logger.info(f"完成: {title}")

    except Exception as e:
        logger.error(f"处理任务失败: {e}", exc_info=True)
        try:
            await send_text_message(
                user_id,
                user_id,
                f"处理失败: {str(e)[:200]}\n\n请检查链接是否有效，或稍后重试。"
            )
        except Exception:
            pass

    finally:
        # 清理
        if video_id:
            try:
                # 保留 PDF 用于调试? 或者也清理
                cleanup_files(video_id)
                if os.path.exists(os.path.join(TEMP_DIR, f"{video_id}_summary.pdf")):
                     os.remove(os.path.join(TEMP_DIR, f"{video_id}_summary.pdf"))
            except Exception:
                pass
        _pending.pop(user_id, None)


async def _send_file_message(user_id: str, media_id: str):
    """发送文件消息"""
    from wechat_api import get_access_token
    import httpx
    from config import AGENT_ID
    
    token = await get_access_token()
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    
    payload = {
        "touser": user_id,
        "msgtype": "file",
        "agentid": AGENT_ID,
        "file": {"media_id": media_id},
    }
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload)
        data = resp.json()
        if data.get("errcode") != 0:
            logger.error(f"发送文件消息失败: {data}")
            raise Exception(f"发送文件失败: {data.get('errmsg')}")


# ========================
# 辅助接口
# ========================

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "pending_tasks": len(_pending),
        "service": "douyin-video-summarizer",
    }


@app.get("/")
async def root():
    return {"message": "抖音视频知识总结 Bot 运行中"}


# ========================
# 启动
# ========================
if __name__ == "__main__":
    import uvicorn
    from config import SERVER_HOST, SERVER_PORT
    uvicorn.run("main:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)
