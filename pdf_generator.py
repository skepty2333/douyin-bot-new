"""
PDF 生成模块
将 Markdown 总结转换为高质量 PDF (WeasyPrint 渲染)

特性:
- LaTeX 公式渲染 (matplotlib)
- 鲁棒的 Markdown 预处理 (处理 AI 生成的格式缺陷)
- GitHub 风格样式 (对标 Markdown Viewer 扩展的 GitHub 主题)
"""
import logging
import re
import base64
import io
import os
import tempfile
import gc

logger = logging.getLogger(__name__)


# ============================================================
# AI 输出清理 (移除思维链、搜索标签等)
# ============================================================

def cleanup_ai_output(content: str) -> str:
    """
    清理 AI 输出中不应展示的内容：
    - <search>...</search> 标签
    - <query>...</query> 标签
    - AI 开场白 (如 "I'll search for...")
    - 空的引用块开头
    """
    # 0. 激进清理: 如果包含 <search> 标签，直接丢弃第一个 <search> 之前的所有内容
    # 这通常是 AI 的思维链开场白 (e.g. "I'll now execute...")
    search_start = content.find('<search>')
    if search_start != -1:
        content = content[search_start:]
    
    # 移除 <search>...</search> 块
    content = re.sub(r'<search>.*?</search>\s*', '', content, flags=re.DOTALL)
    
    # 移除 <query>...</query> 标签
    content = re.sub(r'<query>.*?</query>\s*', '', content, flags=re.DOTALL)
    
    # (旧逻辑已不再需要，但保留以防万一没有 search 标签)
    # 移除 AI 思维链开场白 (查找第一个 Markdown 标题之前的非标题内容)
    
    # 移除开头的空行
    content = content.lstrip('\n')
    
    return content

# ============================================================
# LaTeX 渲染器 (matplotlib)
# ============================================================

def render_latex_to_base64(latex_code: str, fontsize: int = 12, dpi: int = 72) -> str:
    """
    使用 matplotlib 将 LaTeX 公式渲染为 base64 PNG 图片
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        import matplotlib.pyplot as plt # Keep for rcParams

        # Use 'custom' fontset to support CJK in mathtext (LaTeX) without 'tofu'
        # We use 'Noto Sans CJK JP' because it is discoverable by system font manager
        # even without explicit addfont (which causes OOM)
        plt.rcParams['mathtext.fontset'] = 'custom'
        plt.rcParams['mathtext.rm'] = 'Noto Sans CJK JP'
        plt.rcParams['mathtext.it'] = 'Noto Sans CJK JP:italic'
        plt.rcParams['mathtext.bf'] = 'Noto Sans CJK JP:bold'

        # Configure normal text font priority
        plt.rcParams['font.family'] = 'sans-serif'
        plt.rcParams['font.sans-serif'] = ['Noto Sans CJK SC', 'Noto Sans CJK JP', 'SimHei', 'Arial', 'sans-serif']
        plt.rcParams['axes.unicode_minus'] = False     

        # 预处理 LaTeX：替换 matplotlib 不支持的命令
        latex = _preprocess_latex(latex_code)
        
        # 移除 None 检查，尝试渲染所有公式

        # Use object-oriented API to avoid memory leaks from pyplot state machine
        fig = Figure(figsize=(0.01, 0.01))
        fig.patch.set_alpha(0)
        FigureCanvasAgg(fig) # Attach canvas

        # 使用 mathtext 渲染，但不用 math_fontfamily='cm' 以便使用自定义字体
        # 注意: matplotlib 的 mathtext 对中文支持有限，可能需要将中文放在 \text{} 中 (已替换为 \mathrm)
        # 尝试使用普通文本渲染模式如果包含中文
        text = fig.text(0, 0, f"${latex}$", fontsize=fontsize, usetex=False)

        fig.canvas.draw()
        bbox = text.get_window_extent()

        width = bbox.width / fig.dpi + 0.1
        height = bbox.height / fig.dpi + 0.1
        fig.set_size_inches(width, height)
        text.set_position((0.05, 0.2))

        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=dpi,
                    bbox_inches='tight', pad_inches=0.02,
                    transparent=True)
        # No plt.close(fig) needed for Figure object
        gc.collect()

        fig.canvas.draw()
        bbox = text.get_window_extent()

        width = bbox.width / fig.dpi + 0.1
        height = bbox.height / fig.dpi + 0.1
        fig.set_size_inches(width, height)
        text.set_position((0.05, 0.2))

        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=dpi,
                    bbox_inches='tight', pad_inches=0.02,
                    transparent=True)
        # No plt.close(fig) needed for Figure object
        gc.collect()

        buf.seek(0)
        return base64.b64encode(buf.read()).decode('utf-8')

    except Exception as e:
        logger.warning(f"LaTeX 渲染失败: {e}")
        return None


def _preprocess_latex(latex: str) -> str:
    """
    预处理 LaTeX 公式，替换 matplotlib 不支持的命令
    """
    # \text{} -> \mathrm{} (matplotlib 不支持 \text)
    latex = re.sub(r'\\text\{([^}]*)\}', r'\\mathrm{\1}', latex)
    
    # 移除之前的中文检测拦截，让 matplotlib 尝试渲染
    
    return latex


# ============================================================
# LaTeX 公式处理
# ============================================================

def _fix_blockquote_latex(content: str) -> str:
    """
    预处理引用块中的 LaTeX 公式。
    
    问题：引用块中的 LaTeX 如：
        > $$\\text{Profit Factor} = ...$$
    
    由于 $$ 前有 '> ' 前缀，正则 r'\\$\\$(.+?)\\$\\$' 可能无法正确匹配。
    
    解决方案：先将引用块中的 $$...$$ 提取出来，渲染后放回。
    """
    lines = content.split('\n')
    result_lines = []
    
    for line in lines:
        # 检查是否是引用块中的 LaTeX 行
        if line.strip().startswith('>'):
            # 提取引用前缀和内容
            match = re.match(r'^(\s*>\s*)', line)
            if match:
                prefix = match.group(1)
                rest = line[len(prefix):]
                
                # 处理块级 LaTeX: $$...$$
                if '$$' in rest:
                    rest = re.sub(
                        r'\$\$(.+?)\$\$',
                        lambda m: _render_latex_inline(m.group(1), block=True),
                        rest,
                        flags=re.DOTALL
                    )
                
                # 处理行内 LaTeX: $...$
                if '$' in rest:
                    rest = re.sub(
                        r'(?<!\$)\$([^\$\n]+?)\$(?!\$)',
                        lambda m: _render_latex_inline(m.group(1), block=False),
                        rest
                    )
                
                line = prefix + rest
        
        result_lines.append(line)
    
    return '\n'.join(result_lines)


def _render_latex_inline(latex_code: str, block: bool = False) -> str:
    """渲染单个 LaTeX 公式并返回 HTML img 标签"""
    latex = latex_code.strip()
    if not latex:
        return f'$${latex_code}$$' if block else f'${latex_code}$'
    
    b64 = render_latex_to_base64(latex, fontsize=10, dpi=72)
    if b64:
        if block:
            return (f'<img src="data:image/png;base64,{b64}" '
                    f'alt="formula" class="block-formula" style="display:block;margin:0 auto;"/>')
        else:
            return (f'<img src="data:image/png;base64,{b64}" '
                    f'alt="formula" class="inline-formula"/>')
    else:
        return f'<code>{latex}</code>'

def process_latex_in_markdown(content: str) -> str:
    """
    查找 Markdown 中的 LaTeX 公式并替换为内嵌图片

    支持:
    - 行内公式: $...$
    - 块级公式: $$...$$ 或 ```math ... ```
    """
    def replace_block_latex(match):
        latex = match.group(1).strip()
        b64 = render_latex_to_base64(latex, fontsize=10, dpi=72)
        if b64:
            return (f'\n\n<p style="text-align:center;">'
                    f'<img src="data:image/png;base64,{b64}" '
                    f'alt="formula" class="block-formula"/></p>\n\n')
        return f'\n\n<pre><code>{latex}</code></pre>\n\n'

    def replace_math_block(match):
        latex = match.group(1).strip()
        b64 = render_latex_to_base64(latex, fontsize=10, dpi=72)
        if b64:
            return (f'\n\n<p style="text-align:center;">'
                    f'<img src="data:image/png;base64,{b64}" '
                    f'alt="formula" class="block-formula"/></p>\n\n')
        return f'\n\n<pre><code>{latex}</code></pre>\n\n'

    def replace_inline_latex(match):
        latex = match.group(1).strip()
        if not latex:
            return match.group(0)
        b64 = render_latex_to_base64(latex, fontsize=10, dpi=72)
        if b64:
            return (f'<img src="data:image/png;base64,{b64}" '
                    f'alt="formula" class="inline-formula"/>')
        return f'<code>{latex}</code>'

    # 处理顺序: 块级 → 代码块 → 行内 (避免冲突)
    content = re.sub(r'\$\$(.+?)\$\$', replace_block_latex, content, flags=re.DOTALL)
    content = re.sub(r'```math\s*\n(.+?)\n```', replace_math_block, content, flags=re.DOTALL)
    content = re.sub(r'(?<!\$)\$([^\$\n]+?)\$(?!\$)', replace_inline_latex, content)

    return content


# ============================================================
# Markdown 预处理 (鲁棒处理 AI 生成的格式缺陷)
# ============================================================

# "内容结束字符": 这些字符后跟 ` * ` 时，几乎一定是列表项分隔
# 包括: CJK 汉字、全角标点、右括号/引号、ASCII 字母数字、百分号等
_CONTENT_END_RE = re.compile(
    r'[\u4e00-\u9fff'         # CJK 统一汉字
    r'\u3400-\u4dbf'          # CJK 扩展 A
    r'\uff01-\uff60'          # 全角标点和字母 (！～）等)
    r'\u3000-\u303f'          # CJK 标点 (、。等)
    r'a-zA-Z'                 # ASCII 字母 (不含数字，避免误伤 "5 * 3")
    r')\]>»）】》\u300b'      # 右括号/右书名号
    r'\'""\u201d\u300d'       # 右引号
    r'\.!?\?？。！%‰'         # 句末标点 & 百分号
    r']'
)

# 列表标记正则 (无序: *, -, +  有序: 1. 2. ...)
_UL_MARKER_RE = re.compile(r'^[*\-+]([ \t]+)')
_OL_MARKER_RE = re.compile(r'^\d{1,3}\.([ \t]+)')


def _match_list_marker(text: str):
    """检查 text 开头是否是列表标记，返回 Match 或 None"""
    m = _UL_MARKER_RE.match(text)
    if m:
        return m
    return _OL_MARKER_RE.match(text)


def _paren_depth_at(text: str, pos: int) -> int:
    """计算 text[0:pos] 中未闭合的括号深度 (中英文括号都算)"""
    depth = 0
    for i in range(pos):
        c = text[i]
        if c in '(（[【':
            depth += 1
        elif c in ')）]】':
            depth = max(0, depth - 1)
    return depth


def _split_inline_list_items(line: str) -> str:
    """
    将单行内被连接的多个列表项拆分为多行。

    核心策略 (逐字符扫描):
    - 在行中寻找 "空白 + 列表标记 + 空白" 的模式
    - 只有当标记前一个非空字符是"内容结束字符"时才分行
    - 不在括号内部分行 (避免拆分 "(收益 - 成本)" 等)
    - 不拆分数字后的标记 (避免拆分 "5 * 3")

    示例:
    输入: "系统性风险 * 非系统性风险"
    输出: "系统性风险\\n\\n* 非系统性风险"
    """
    if not line or line.startswith('#'):
        return line

    parts = []
    remaining = line

    # 跳过行首已有的列表标记
    head_marker = _match_list_marker(remaining)
    if head_marker:
        scan_start = head_marker.end()
    else:
        scan_start = 0

    while scan_start < len(remaining):
        match = re.search(
            r'([ \t]+)(?=[*\-+][ \t]|\d{1,3}\.[ \t])',
            remaining[scan_start:]
        )
        if not match:
            break

        ws_start = scan_start + match.start()
        marker_start = scan_start + match.end()

        # 三重检查: 前字符匹配 AND 不在括号内 AND 有后续内容
        if (ws_start > 0
                and _CONTENT_END_RE.match(remaining[ws_start - 1])
                and _paren_depth_at(remaining, ws_start) == 0):
            marker_match = _match_list_marker(remaining[marker_start:])
            if marker_match:
                after_marker_pos = marker_start + marker_match.end()
                after_content = remaining[after_marker_pos:].strip()
                if after_content:
                    parts.append(remaining[:ws_start])
                    remaining = remaining[marker_start:]
                    head_marker = _match_list_marker(remaining)
                    scan_start = head_marker.end() if head_marker else 0
                    continue

        scan_start = marker_start + 1

    parts.append(remaining)
    return '\n\n'.join(parts)


def _fix_colon_then_list(line: str) -> str:
    """
    处理 "文字：* 列表" 或 "文字：1. 列表" 模式。
    在冒号和列表标记之间插入分行。
    """
    # 中/英冒号 + 可选空白 + 无序列表标记
    m = re.match(r'^(.*[：:])(\s*)([*\-+][ \t]+.+)$', line)
    if m:
        return m.group(1) + '\n\n' + m.group(3)

    # 中/英冒号 + 可选空白 + 有序列表标记
    m = re.match(r'^(.*[：:])(\s*)(\d{1,3}\.[ \t]+.+)$', line)
    if m:
        return m.group(1) + '\n\n' + m.group(3)

    return line


def _fix_marker_spacing(line: str) -> str:
    """
    修复缺少空格的列表标记: "*文字" → "* 文字"
    仅处理行首。
    """
    if re.match(r'^\*[^\s*]', line):
        return '* ' + line[1:]
    if re.match(r'^-[^\s\-]', line):
        return '- ' + line[1:]
    if re.match(r'^\+[^\s+]', line):
        return '+ ' + line[1:]
    return line


def normalize_markdown(content: str) -> str:
    """
    对 Markdown (尤其是 AI 生成的) 进行鲁棒的格式修正。

    处理的问题:
    1. 换行符不一致 (\\r\\n / \\r)
    2. 列表项被连接在同一行 ("item1 * item2 * item3")
    3. 冒号后直接跟列表标记 ("分类：* 项目1")
    4. 列表标记缺少空格 ("*文字")

    设计原则:
    - 基于字符级扫描 + 上下文判断，而非一刀切的正则
    - 只在"内容结束字符"后的列表标记处分行，避免拆分数学表达式
    - 保护 LaTeX 公式内容 ($...$ 和 $$...$$) 不被修改
    - 保持幂等: 对已正确格式化的内容不做任何修改
    """
    # Step 0: 统一换行符
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    # Step 0.5: 保护 LaTeX 公式 — 用占位符替换，避免列表修正误伤
    latex_store = []

    def _stash_latex(match):
        idx = len(latex_store)
        latex_store.append(match.group(0))
        return f'\x00LATEX{idx}\x00'

    # 先保护块级 $$...$$ (贪婪匹配到最近的 $$)
    content = re.sub(r'\$\$.+?\$\$', _stash_latex, content, flags=re.DOTALL)
    # 再保护行内 $...$
    content = re.sub(r'(?<!\$)\$[^\$\n]+?\$(?!\$)', _stash_latex, content)
    # 保护 ```math ... ``` 代码块
    content = re.sub(r'```math\s*\n.+?\n```', _stash_latex, content, flags=re.DOTALL)

    # Step 1: 逐行处理
    output_lines = []
    in_code_block = False

    for line in content.split('\n'):
        # 代码块内不做任何处理
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            output_lines.append(line)
            continue

        if in_code_block:
            output_lines.append(line)
            continue

        stripped = line.strip()

        # 空行 / 标题 / 引用 / 分割线: 直接保留
        if not stripped or stripped.startswith(('#', '>', '---', '===')):
            output_lines.append(line)
            continue

        # a) 冒号后直接跟列表 → 分行
        line = _fix_colon_then_list(line)

        # b) 行内连续列表项 → 分行 (对 a 产生的多行分别处理)
        sub_lines = line.split('\n')
        expanded = []
        for sl in sub_lines:
            expanded.append(_split_inline_list_items(sl))
        line = '\n'.join(expanded)

        # c) 缺少空格的列表标记 → 补空格
        sub_lines = line.split('\n')
        fixed = []
        for sl in sub_lines:
            fixed.append(_fix_marker_spacing(sl))
        line = '\n'.join(fixed)

        output_lines.append(line)

    result = '\n'.join(output_lines)

    # Step 2: 恢复 LaTeX 公式
    for idx, original in enumerate(latex_store):
        result = result.replace(f'\x00LATEX{idx}\x00', original)

    # Step 3: 确保列表项前有空行 (Markdown 解析器需要空行才能识别列表)
    result = _ensure_blank_before_list(result)

    return result


def _ensure_blank_before_list(content: str) -> str:
    """
    确保所有列表项前面有空行，否则 markdown 库会把它们和前面的段落合并。
    
    处理模式：
    - 以冒号结尾的行后面紧跟 * 或 - 列表项
    - 普通段落后面紧跟 * 或 - 列表项
    """
    lines = content.split('\n')
    result = []
    
    for i, line in enumerate(lines):
        result.append(line)
        
        # 如果这不是最后一行
        if i < len(lines) - 1:
            next_line = lines[i + 1].strip()
            current_stripped = line.strip()
            
            # 如果下一行是列表项 (*, -, +, 或数字.)
            is_next_list = bool(re.match(r'^[*\-+]\s+', next_line) or re.match(r'^\d+\.\s+', next_line))
            
            if is_next_list:
                # 当前行是空行则不需要处理
                if not current_stripped:
                    continue
                    
                # 如果当前行以冒号结尾（中文或英文），或是普通文本行
                # 并且下一行是列表项，则插入空行
                if (current_stripped.endswith(('：', ':')) or
                    (not current_stripped.startswith(('#', '>', '-', '*', '+')) and
                     not re.match(r'^\d+\.', current_stripped))):
                    result.append('')
    
    return '\n'.join(result)


# ============================================================
# CSS 样式 — 对标 Markdown Viewer (GitHub 主题 + markdown-it)
# ============================================================
# 参考: sindresorhus/github-markdown-css + simov/markdown-themes
# 特点: 980px 居中卡片、左边框引用、h1/h2 底部分割线、1.5 行高

GITHUB_PDF_CSS = """
/* === 页面 === */
@page {
    margin: 20mm 15mm;
}

/* === 正文容器 === */
body {
    font-family: 'Noto Sans CJK SC', 'SimHei', -apple-system,
                 BlinkMacSystemFont, "Segoe UI", "Noto Sans",
                 Helvetica, Arial, sans-serif;
    font-size: 14px;
    line-height: 1.6;
    color: #1f2328;
    word-wrap: break-word;
    background-color: #ffffff;
    max-width: 980px;
    margin: 0 auto;
    padding: 45px;
    border: 1px solid #d1d9e0;
    border-radius: 6px;
}

/* === 标题 === */
h1, h2, h3, h4, h5, h6 {
    margin-top: 24px;
    margin-bottom: 16px;
    font-weight: 600;
    line-height: 1.25;
    color: #1f2328;
}

h1 {
    font-size: 2em;
    padding-bottom: 0.3em;
    border-bottom: 1px solid #d1d9e0;
}

h2 {
    font-size: 1.5em;
    padding-bottom: 0.3em;
    border-bottom: 1px solid #d1d9e0;
}

h3 { font-size: 1.25em; }
h4 { font-size: 1em; }
h5 { font-size: 0.875em; }
h6 { font-size: 0.85em; color: #656d76; }

/* === 段落 === */
p {
    margin-top: 0;
    margin-bottom: 16px;
}

/* === 链接 === */
a {
    color: #0969da;
    text-decoration: none;
}

/* === 加粗文字 (SimHei 无粗体变体，使用合成粗体) === */
strong, b { 
    font-weight: bold;
    letter-spacing: 0.02em;
}
em, i { font-style: italic; }

/* === 列表 === */
ul, ol {
    padding-left: 2em;
    margin-top: 0;
    margin-bottom: 16px;
}

li {
    margin-top: 0.25em;
}

li > ul, li > ol {
    margin-bottom: 0;
}

/* === 引用块 — GitHub 经典左边框 === */
blockquote {
    margin: 0 0 16px 0;
    padding: 0 1em;
    color: #656d76;
    border-left: 0.25em solid #d1d9e0;
}

blockquote > :first-child { margin-top: 0; }
blockquote > :last-child { margin-bottom: 0; }

/* === 行内代码 === */
code {
    padding: 0.2em 0.4em;
    margin: 0;
    font-size: 85%;
    white-space: break-spaces;
    background-color: rgba(175, 184, 193, 0.2);
    border-radius: 6px;
    font-family: ui-monospace, SFMono-Regular, "SF Mono",
                 Menlo, Consolas, "Liberation Mono", monospace;
}

/* === 代码块 === */
pre {
    padding: 16px;
    overflow: auto;
    font-size: 85%;
    line-height: 1.45;
    color: #1f2328;
    background-color: #f6f8fa;
    border-radius: 6px;
    margin-bottom: 16px;
    word-wrap: normal;
}

pre code {
    padding: 0;
    margin: 0;
    font-size: 100%;
    white-space: pre;
    background: transparent;
    border: 0;
}

/* === 表格 === */
table {
    border-spacing: 0;
    border-collapse: collapse;
    margin-bottom: 16px;
    display: table;
    width: max-content;
    max-width: 100%;
}

th, td {
    padding: 6px 13px;
    border: 1px solid #d1d9e0;
}

th { font-weight: 600; }

tr {
    background-color: #ffffff;
    border-top: 1px solid #d1d9e0;
}

tr:nth-child(2n) {
    background-color: #f6f8fa;
}

/* === 水平线 === */
hr {
    height: 0.25em;
    padding: 0;
    margin: 24px 0;
    background-color: #d1d9e0;
    border: 0;
}

/* === 图片 === */
img { max-width: 100%; box-sizing: border-box; }

/* === 公式: 块级 (居中显示) === */
img.block-formula {
    display: inline-block;
    height: auto;
    width: auto;
    vertical-align: middle;
}

/* === 公式: 行内 (与文字对齐) === */
img.inline-formula {
    display: inline-block;
    height: auto;
    width: auto;
    vertical-align: middle;
}
"""


# ============================================================
# PDF 生成主函数
# ============================================================

def generate_pdf(markdown_content: str, output_path: str) -> bool:
    """
    Markdown → PDF (带 LaTeX 渲染)
    返回 True/False 表示是否成功
    """
    try:
        import markdown
        from weasyprint import HTML, CSS

        # Step 1: 清理 AI 输出 (移除思维链、搜索标签)
        content = cleanup_ai_output(markdown_content)
        
        # Step 2: 预处理引用块中的 LaTeX (移除 > 前缀以便正则匹配)
        # 将引用块中的 $$...$$ 提取出来单独处理
        content = _fix_blockquote_latex(content)
        
        # Step 3: 渲染 LaTeX 公式为图片 (必须在 normalize 之前，否则占位符会被处理)
        content = process_latex_in_markdown(content)

        # Step 4: 鲁棒格式修正
        content = normalize_markdown(content)

        # Markdown → HTML
        html_content = markdown.markdown(
            content,
            extensions=['extra', 'tables', 'fenced_code', 'sane_lists'],
        )

        full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>文档</title></head>
<body class="markdown-body">{html_content}</body>
</html>"""

        html = HTML(string=full_html)
        css = CSS(string=GITHUB_PDF_CSS)
        html.write_pdf(output_path, stylesheets=[css])

        logger.info(f"PDF 生成成功: {output_path}")
        return True

    except Exception as e:
        logger.error(f"PDF 生成失败: {e}")
        return False
