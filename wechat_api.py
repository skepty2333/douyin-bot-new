"""
企业微信 API 封装 - 主动发送消息
"""
import time
import logging
import httpx
from config import CORP_ID, CORP_SECRET, AGENT_ID

logger = logging.getLogger(__name__)

# Access Token 缓存
_access_token = ""
_token_expires_at = 0


async def get_access_token() -> str:
    """获取 access_token，带缓存"""
    global _access_token, _token_expires_at

    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token

    url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    params = {"corpid": CORP_ID, "corpsecret": CORP_SECRET}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params)
        data = resp.json()

    if data.get("errcode") != 0:
        logger.error(f"获取access_token失败: {data}")
        raise Exception(f"获取access_token失败: {data.get('errmsg')}")

    _access_token = data["access_token"]
    _token_expires_at = time.time() + data.get("expires_in", 7200)
    logger.info("access_token 刷新成功")
    return _access_token


async def send_text_message(user_id: str, content: str):
    """发送文本消息给用户"""
    token = await get_access_token()
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"

    # 企业微信文本消息最大长度2048字节
    # 如果内容过长，分段发送
    max_len = 2000
    parts = []
    while content:
        if len(content.encode('utf-8')) <= max_len:
            parts.append(content)
            break
        # 按字符截断，确保不超过字节限制
        cut = max_len
        while len(content[:cut].encode('utf-8')) > max_len:
            cut -= 1
        # 尝试在换行符处截断
        last_newline = content[:cut].rfind('\n')
        if last_newline > cut // 2:
            cut = last_newline + 1
        parts.append(content[:cut])
        content = content[cut:]

    async with httpx.AsyncClient() as client:
        for i, part in enumerate(parts):
            if len(parts) > 1:
                part = f"[{i+1}/{len(parts)}]\n{part}" if i > 0 else part

            payload = {
                "touser": user_id,
                "msgtype": "text",
                "agentid": AGENT_ID,
                "text": {"content": part},
            }
            resp = await client.post(url, json=payload)
            data = resp.json()
            if data.get("errcode") != 0:
                logger.error(f"发送消息失败: {data}")
            else:
                logger.info(f"消息发送成功 -> {user_id} (part {i+1}/{len(parts)})")


async def send_markdown_message(user_id: str, content: str):
    """
    发送Markdown消息给用户 (仅企业微信内可渲染)
    支持: 标题、加粗、链接、引用、字体颜色等
    自动分段发送，避免超过2048字节限制
    """
    token = await get_access_token()
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"

    # 企业微信限制: 2048字节 (UTF-8)
    # 安全起见设为 1800
    MAX_BYTES = 1800
    
    parts = []
    current_part = ""
    
    # 简单按段落切分，避免破坏Markdown格式
    paragraphs = content.split('\n')
    
    for p in paragraphs:
        # 加上换行符
        line = p + '\n'
        if len((current_part + line).encode('utf-8')) > MAX_BYTES:
            if current_part:
                parts.append(current_part)
            current_part = line
        else:
            current_part += line
            
    if current_part:
        parts.append(current_part)

    async with httpx.AsyncClient() as client:
        for i, part in enumerate(parts):
            # 如果分段了，可以考虑加页码，但Markdown加页码可能会破坏美感
            # 这里选择直接发送，依靠用户端自然的顺序显示
            
            payload = {
                "touser": user_id,
                "msgtype": "markdown",
                "agentid": AGENT_ID,
                "markdown": {"content": part},
            }
            
            try:
                resp = await client.post(url, json=payload)
                data = resp.json()
                if data.get("errcode") != 0:
                    logger.error(f"发送Markdown消息失败 (part {i+1}/{len(parts)}): {data}")
                else:
                    logger.info(f"Markdown消息发送成功 -> {user_id} (part {i+1}/{len(parts)})")
            except Exception as e:
                logger.error(f"发送请求异常: {e}") 
                
            # 稍微间隔一下，避免顺序错乱
            if len(parts) > 1:
                await asyncio.sleep(0.2)


async def upload_temp_media(file_path: str, media_type: str = "file") -> str:
    """
    上传临时素材，返回 media_id
    media_type: image/voice/video/file
    """
    token = await get_access_token()
    url = f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={token}&type={media_type}"

    async with httpx.AsyncClient() as client:
        with open(file_path, "rb") as f:
            files = {"media": f}
            resp = await client.post(url, files=files)
            data = resp.json()

    if data.get("errcode") and data["errcode"] != 0:
        logger.error(f"上传素材失败: {data}")
        raise Exception(f"上传素材失败: {data.get('errmsg')}")

    return data.get("media_id", "")
