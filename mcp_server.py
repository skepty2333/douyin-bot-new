"""
抖音知识库 MCP Server

暴露知识库搜索/检索工具给 Claude (通过 Connector 功能)

部署后在 claude.ai → Settings → Connectors → Add Custom Connector
填入: http://你的IP:8090/mcp

Tools:
  - search_notes:   全文搜索知识库
  - get_note:       获取完整笔记内容
  - list_notes:     列出最近笔记
  - list_by_tag:    按标签筛选
  - get_note_by_code: 通过视频码获取笔记
  - knowledge_stats: 数据库统计
"""
import os
import json
import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from knowledge_store import KnowledgeStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-knowledge")

# 初始化
DB_PATH = os.getenv("KNOWLEDGE_DB_PATH", "/home/admin/douyin-bot/knowledge.db")
store = KnowledgeStore(DB_PATH)

# 禁用 DNS rebinding 保护 (允许 Cloudflare Tunnel 访问)
security_settings = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp = FastMCP("douyin_knowledge_mcp", host="0.0.0.0", port=8090, transport_security=security_settings)


# ============================================================
# Tool 1: 搜索笔记
# ============================================================

class SearchInput(BaseModel):
    """搜索知识库的输入参数"""
    query: str = Field(
        ...,
        description="搜索关键词，支持中文。例如: '量化交易', '投资策略', 'AI工程师'",
        min_length=1,
        max_length=200,
    )
    limit: int = Field(
        default=5,
        description="返回结果数量上限",
        ge=1,
        le=20,
    )


@mcp.tool(
    name="search_notes",
    annotations={
        "title": "搜索视频笔记",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def search_notes(params: SearchInput) -> str:
    """在知识库中搜索视频学习笔记。

    使用全文搜索在所有已保存的抖音视频总结中查找匹配内容。
    搜索范围包括: 标题、作者、笔记内容、标签。
    返回匹配结果列表和内容摘要片段。

    Args:
        params (SearchInput): 搜索参数
            - query (str): 搜索关键词
            - limit (int): 结果数量上限

    Returns:
        str: Markdown 格式的搜索结果列表
    """
    results = store.search(params.query, params.limit)

    if not results:
        return f"未找到与 \"{params.query}\" 相关的笔记。"

    lines = [f"## 搜索结果: \"{params.query}\" ({len(results)} 条)\n"]
    for r in results:
        lines.append(
            f"### [{r['id']}] {r['title']}\n"
            f"- **视频码**: `{r['video_code']}`\n"
            f"- **作者**: {r['author']}\n"
            f"- **标签**: {r['tags'][:100]}\n"
            f"- **时间**: {r['created_at'][:10]}\n"
            f"- **摘要**: {r.get('snippet', '')[:200]}\n"
        )
    lines.append("\n> 💡 使用 `get_note` (ID) 或 `get_note_by_code` (视频码) 获取完整内容。")
    return "\n".join(lines)


# ============================================================
# Tool 2: 获取完整笔记
# ============================================================

class GetNoteInput(BaseModel):
    """获取笔记的输入参数"""
    note_id: int = Field(
        ...,
        description="笔记的 ID 编号（从 search_notes 或 list_notes 结果中获取）",
        ge=1,
    )


@mcp.tool(
    name="get_note",
    annotations={
        "title": "获取完整笔记",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_note(params: GetNoteInput) -> str:
    """获取一条视频笔记的完整 Markdown 内容。

    返回包括标题、作者、标签、用户要求以及完整的 AI 总结笔记。

    Args:
        params (GetNoteInput): 包含笔记 ID

    Returns:
        str: 完整的笔记内容 (Markdown 格式)
    """
    entry = store.get_by_id(params.note_id)
    if not entry:
        return f"❌ 未找到 ID 为 {params.note_id} 的笔记。"

    header = (
        f"# {entry['title']}\n\n"
        f"- **作者**: {entry['author']}\n"
        f"- **来源**: {entry['source_url']}\n"
        f"- **标签**: {entry['tags']}\n"
        f"- **创建时间**: {entry['created_at']}\n"
    )
    if entry.get('user_requirement'):
        header += f"- **用户要求**: {entry['user_requirement']}\n"

    header += "\n---\n\n"

    return header + entry['summary_markdown']

@mcp.tool(
    name="get_note_by_code",
    annotations={
        "title": "通过视频码获取笔记",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_note_by_code(video_code: str) -> str:
    """通过 5位视频码 获取完整笔记内容。
    
    Args:
        video_code (str): 5位视频码 (例如: 'a1b2c')
    """
    entry = store.get_by_video_code(video_code)
    if not entry:
        return f"❌ 未找到视频码为 {video_code} 的笔记。"

    header = (
        f"# {entry['title']}\n\n"
        f"- **视频码**: `{entry['video_code']}`\n"
        f"- **作者**: {entry['author']}\n"
        f"- **来源**: {entry['source_url']}\n"
        f"- **标签**: {entry['tags']}\n"
        f"- **创建时间**: {entry['created_at']}\n"
    )
    if entry.get('user_requirement'):
        header += f"- **用户要求**: {entry['user_requirement']}\n"

    header += "\n---\n\n"

    return header + entry['summary_markdown']
# ============================================================
# Tool 3: 列出最近笔记
# ============================================================

class ListNotesInput(BaseModel):
    """列出笔记的输入参数"""
    limit: int = Field(
        default=10,
        description="返回数量",
        ge=1,
        le=50,
    )
    offset: int = Field(
        default=0,
        description="跳过前 N 条（分页用）",
        ge=0,
    )


@mcp.tool(
    name="list_notes",
    annotations={
        "title": "列出最近笔记",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_notes(params: ListNotesInput) -> str:
    """列出知识库中最近保存的视频笔记。

    按时间倒序返回笔记列表（标题、作者、标签、时间）。
    不含完整内容，需要内容请用 get_note。

    Args:
        params (ListNotesInput): 分页参数

    Returns:
        str: Markdown 格式的笔记列表
    """
    notes = store.list_recent(params.limit, params.offset)

    if not notes:
        return "知识库暂无笔记。"

    lines = [f"## 最近笔记 (第 {params.offset+1}-{params.offset+len(notes)} 条)\n"]
    for n in notes:
        lines.append(
            f"- **[{n['id']}]** `{n['video_code']}` {n['title']} — _{n['author']}_ "
            f"({n['created_at'][:10]})"
        )
        if n['tags']:
            lines.append(f"  标签: {n['tags'][:80]}")

    return "\n".join(lines)


# ============================================================
# Tool 4: 按标签筛选
# ============================================================

class TagFilterInput(BaseModel):
    """按标签筛选的输入参数"""
    tag: str = Field(
        ...,
        description="标签关键词，例如: '量化交易', 'AI', '投资'",
        min_length=1,
    )
    limit: int = Field(default=10, ge=1, le=50)


@mcp.tool(
    name="list_by_tag",
    annotations={
        "title": "按标签筛选笔记",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_by_tag(params: TagFilterInput) -> str:
    """按标签筛选知识库中的笔记。

    Args:
        params (TagFilterInput): 标签和数量

    Returns:
        str: 匹配的笔记列表
    """
    notes = store.list_by_tag(params.tag, params.limit)
    if not notes:
        return f"未找到包含标签 \"{params.tag}\" 的笔记。"

    lines = [f"## 标签 \"{params.tag}\" 相关笔记 ({len(notes)} 条)\n"]
    for n in notes:
        lines.append(
            f"- **[{n['id']}]** {n['title']} — _{n['author']}_ "
            f"({n['created_at'][:10]})"
        )
    return "\n".join(lines)


# ============================================================
# Tool 5: 统计
# ============================================================

@mcp.tool(
    name="knowledge_stats",
    annotations={
        "title": "知识库统计",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def knowledge_stats() -> str:
    """获取知识库的总体统计信息。

    Returns:
        str: 统计信息 (总条数、最新记录时间等)
    """
    s = store.stats()
    return (
        f"## 知识库统计\n\n"
        f"- **总笔记数**: {s['total_entries']}\n"
        f"- **最新记录**: {s['latest_entry'] or '无'}\n"
        f"- **数据库路径**: {s['db_path']}\n"
    )


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    import sys

    port = int(os.getenv("MCP_PORT", "8090"))

    # 支持 stdio 和 http 两种模式
    if "--stdio" in sys.argv:
        print("MCP Server 启动 (stdio 模式)", file=sys.stderr)
        mcp.run(transport="stdio")
    else:
        print(f"MCP Server 启动 (Streamable HTTP) → http://0.0.0.0:{port}/mcp", file=sys.stderr)
        mcp.run(transport="streamable-http")

