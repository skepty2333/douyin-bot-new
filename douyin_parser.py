"""
抖音视频解析模块 (API/Requests 版)

经测试验证: overseas server + mobile UA + iesdouyin.com + _ROUTER_DATA parsing WORKS.
不再使用重型的 Playwright 浏览器，改用轻量级 HTTP 请求。
"""
import asyncio
import os
import re
import json
import logging
import subprocess
import hashlib
from typing import Optional

import httpx
from config import TEMP_DIR

logger = logging.getLogger(__name__)

# 模拟 iPhone 访问，获取移动端页面数据
MOBILE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 Version/17.0 Mobile/15E148 Safari/604.1'
}

# ============================================================
# 1. 从分享文案中提取抖音链接
# ============================================================

def extract_url_from_text(text: str) -> Optional[str]:
    """
    从抖音分享文案中提取URL
    """
    patterns = [
        r'https?://v\.douyin\.com/[A-Za-z0-9_-]+/?',
        r'https?://www\.douyin\.com/video/\d+',
        r'https?://www\.iesdouyin\.com/share/video/\d+',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            url = match.group(0)
            if 'v.douyin.com' in url and not url.endswith('/'):
                url += '/'
            return url
    return None


def extract_user_requirement(text: str, url: str) -> str:
    """
    提取用户附带的总结要求 (去掉链接和分享模板文案)
    """
    remaining = text.replace(url, " ").strip()

    cleaners = [
        r'\d+\.?\d*\s+',                        # "8.94 "
        r'复制.*?打开.*?抖音[，,]?\s*',            # "复制打开抖音，"
        r'看看[【\[]?[^】\]]*?的作品[】\]]?\s*',    # "看看【xxx的作品】"
        r'#\s*[^\s#]+\s*',                       # "# 量化交易 "
        r'@[^\s@]+\s*',                          # "@用户"
        r'"[^"]*\.\.\.\s*',                      # "巴菲特pr..."
        r'[A-Za-z]{2,4}:[/\\]\s*',               # "MwS:/ "
        r'[a-zA-Z]@[A-Za-z]\.[A-Z]{2}\s*',      # "l@P.XM"
        r'\d{1,2}/\d{1,2}\s*',                   # "12/19"
    ]
    for p in cleaners:
        remaining = re.sub(p, ' ', remaining)

    return re.sub(r'\s+', ' ', remaining).strip()


# ============================================================
# 2. 解析抖音视频 (HTTP 请求版)
# ============================================================

async def resolve_and_download(share_url: str) -> dict:
    """
    解析抖音链接提取视频信息并下载
    """
    os.makedirs(TEMP_DIR, exist_ok=True)
    logger.info(f"解析URL: {share_url}")

    video_url = None
    title = "未知标题"
    author = "未知作者"
    video_id = ""

    async with httpx.AsyncClient(headers=MOBILE_HEADERS, follow_redirects=True, timeout=30) as client:
        # 1. 获取重定向后的URL (获取 video_id)
        try:
            resp = await client.get(share_url)
            final_url = str(resp.url)
            logger.info(f"重定向到: {final_url}")
            
            # 提取视频ID
            # https://www.douyin.com/video/7603260688324954810
            # https://www.iesdouyin.com/share/video/7603260688324954810
            path = final_url.split('?')[0]
            video_id = path.split('/')[-1]
            if not video_id.isdigit():
                 ids = re.findall(r'\d{19}', path)
                 if ids:
                     video_id = ids[0]
            
            if not video_id:
                raise ValueError("无法提取视频ID")
            
            logger.info(f"视频ID: {video_id}")
            
            # 2. 请求移动端分享页 (iesdouyin.com)
            ies_url = f'https://www.iesdouyin.com/share/video/{video_id}'
            logger.info(f"请求API页面: {ies_url}")
            
            resp = await client.get(ies_url)
            html = resp.text
            
            # 3. 提取 _ROUTER_DATA JSON 数据
            pattern = re.compile(
                pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
                flags=re.DOTALL,
            )
            match = pattern.search(html)
            
            if match:
                data = json.loads(match.group(1).strip())
                loader_data = data.get("loaderData", {})
                
                video_info = None
                # key 是字面量 "video_(id)/page"
                if "video_(id)/page" in loader_data:
                    video_info = loader_data["video_(id)/page"]["videoInfoRes"]
                elif "note_(id)/page" in loader_data:
                    video_info = loader_data["note_(id)/page"]["videoInfoRes"]
                
                if video_info:
                    item = video_info["item_list"][0]
                    title = item.get("desc", title)
                    
                    # 作者信息
                    if "author" in item:
                         author = item["author"].get("nickname", author)
                    
                    # 视频地址
                    # play_addr -> url_list -> [0]
                    # 需要将 playwm (带水印) 替换为 play (无水印)
                    if "video" in item and "play_addr" in item["video"]:
                        url_list = item["video"]["play_addr"]["url_list"]
                        if url_list:
                             video_url = url_list[0].replace("playwm", "play")
                             # 确保 https
                             if video_url.startswith("//"):
                                 video_url = "https:" + video_url
                else:
                    logger.warning("JSON中未找到 videoInfoRes")
            else:
                logger.warning("_ROUTER_DATA 未找到")
                
        except Exception as e:
            logger.error(f"解析过程出错: {e}")
            raise

    if not video_url:
        raise ValueError("无法获取视频地址 (解析失败)")

    logger.info(f"获取视频地址: {video_url}")
    logger.info(f"标题: {title}")
    logger.info(f"作者: {author}")

    # 4. 下载视频
    video_path = await _download_video(video_url, video_id)

    return {
        "video_id": video_id,
        "title": title,
        "author": author,
        "video_path": video_path,
        "video_url": video_url,
    }


# ============================================================
# 3. 视频下载
# ============================================================

async def _download_video(video_url: str, video_id: str) -> str:
    """流式下载视频"""
    video_path = os.path.join(TEMP_DIR, f"{video_id}.mp4")

    if os.path.exists(video_path) and os.path.getsize(video_path) > 1000:
        logger.info(f"视频已缓存: {video_path}")
        return video_path

    logger.info(f"下载视频: {video_url[:100]}...")

    # 使用移动端 UA 下载，通常更稳定
    async with httpx.AsyncClient(headers=MOBILE_HEADERS, follow_redirects=True, timeout=120) as client:
        async with client.stream("GET", video_url) as resp:
            resp.raise_for_status()
            with open(video_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)

    size_mb = os.path.getsize(video_path) / 1024 / 1024
    logger.info(f"下载完成: {video_path} ({size_mb:.1f}MB)")
    
    # 简单的有效性检查
    if size_mb < 0.5:
        logger.warning(f"下载的文件过小 ({size_mb:.2f}MB), 可能是无效文件")
        
    return video_path


# ============================================================
# 4. 音频提取
# ============================================================

def extract_audio(video_path: str) -> str:
    """ffmpeg 提取音频 (mp3, 16kHz, 单声道)"""
    audio_path = video_path.rsplit(".", 1)[0] + ".mp3"
    if os.path.exists(audio_path):
        return audio_path

    cmd = [
        "ffmpeg", "-i", video_path,
        "-vn", "-acodec", "libmp3lame",
        "-ab", "128k", "-ar", "16000", "-ac", "1",
        "-y", audio_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {proc.stderr[:500]}")

    logger.info(f"音频提取完成: {audio_path}")
    return audio_path


def cleanup_files(video_id: str):
    """清理临时文件"""
    # 简单清理以 video_id 开头的文件
    import glob
    for f in glob.glob(os.path.join(TEMP_DIR, f"{video_id}*")):
        try:
            if os.path.isdir(f):
                pass 
            else:
                os.remove(f)
        except Exception:
            pass
