# 🎬 抖音视频知识总结 Bot

企业微信中发送抖音链接 → AI 三阶段管线自动生成结构化学习笔记 PDF。

## 核心特性

- **三阶段 AI 管线**：Gemini 转写 → DeepSeek 深度审视 → Sonnet 联网搜索增强
- **轻量级解析**：HTTP 请求模拟移动端，无需重型浏览器
- **PDF 输出**：WeasyPrint + Matplotlib 公式渲染，GitHub 风格排版
- **交互式对话**：2分钟窗口期可追加自定义总结要求
- **知识库 & MCP**：内置 SQLite 向量/全文检索，支持 Claude.ai 联网调用知识库

## 架构

```mermaid
graph TD
    User[用户 (企业微信)] -->|1. 抖音链接| Server[Bot主服务]
    Server -->|2. 解析/下载| Parser[抖音解析器]
    Server -->|3. AI总结| AI[三阶段AI管线]
    AI -->|4. 生成PDF| PDF[PDF生成器]
    AI -->|5. 存入SQLite| DB[(知识库 Knowledge.db)]
    
    Claude[Claude.ai] <-->|MCP Protocol| MCPServer[MCP Server :8090]
    MCPServer <-->|Query| DB
    
    Server -->|6. 返回PDF| User
```

## 文件结构

```
douyin-bot/
├── main.py              # FastAPI 主服务 + 会话状态管理
├── config.py            # 环境变量配置
├── ai_summarizer.py     # 三阶段 AI 管线 (Gemini/DeepSeek/Sonnet)
├── pdf_generator.py     # Markdown → PDF (WeasyPrint + Matplotlib)
├── douyin_parser.py     # HTTP 抖音解析 + 视频下载 + 音频提取
├── wechat_crypto.py     # 企业微信消息加解密
├── wechat_api.py        # 企业微信发消息 API
├── requirements.txt     # Python 依赖
├── setup.sh             # 一键部署脚本
├── douyin-bot.service   # systemd 服务配置
└── nginx.conf           # Nginx 反代配置 (可选)
```

## 技术选型

| 环节 | 方案 | 说明 |
|------|------|------|
| 抖音解析 | **HTTP + 移动端UA** | 轻量级，无需浏览器 |
| 音频提取 | **ffmpeg** | 成熟稳定 |
| 阶段1 - 转写 | **Gemini** | 多模态音频理解 |
| 阶段2 - 审视 | **DeepSeek Reasoner** | 深度思考，提出补充问题 |
| 阶段3 - 增强 | **Sonnet 4.5** | 联网搜索，补充背景知识 |
| PDF 生成 | **WeasyPrint** | 纯 Python，支持 CSS |
| 公式渲染 | **Matplotlib** | 内置 mathtext 引擎 |
| Web 框架 | **FastAPI** | 异步原生 |

## 部署

### 1. 上传项目

```bash
scp -r douyin-bot root@服务器IP:~/
ssh root@服务器IP
cd ~/douyin-bot
```

### 2. 一键部署

```bash
chmod +x setup.sh && ./setup.sh
```

自动安装：Python3、ffmpeg、系统依赖、中文字体、Python 包

### 3. 配置环境变量

```bash
nano .env
```

必填项：
```bash
# 企业微信
CORP_ID=ww1234567890abcdef
AGENT_ID=1000002
CORP_SECRET=your_corp_secret
CALLBACK_TOKEN=your_callback_token
CALLBACK_AES_KEY=your_43char_aes_key

# AI API (uiuiapi 聚合平台)
API_BASE_URL=https://sg.uiuiapi.com/v1
GEMINI_API_KEY=your_key
DEEPSEEK_API_KEY=your_key
SONNET_API_KEY=your_key
```

### 4. 启动服务

```bash
sudo systemctl start douyin-bot
sudo systemctl enable douyin-bot

# 开放端口
sudo firewall-cmd --add-port=8080/tcp --permanent
sudo firewall-cmd --reload
```

### 5. 企业微信配置

1. 登录 [企业微信管理后台](https://work.weixin.qq.com)
2. **应用管理 → 自建 → 创建应用**
4. 确保应用可见范围包含你自己

### 6. 部署 Knowledge MCP Server

> **注意**: MCP Server 依赖 `mcp` 库，需要 **Python 3.10+** 环境。当前一键脚本安装的是 Python 3.8/3.9 可能不够，需要自行升级 Python。

```bash
# 确认 Python 版本 >= 3.10
python3 --version

# 安装 MCP 依赖
pip install "mcp[cli]>=1.9.0"

# 启动服务
sudo systemctl start douyin-mcp
sudo systemctl enable douyin-mcp

# 开放端口 8090
sudo firewall-cmd --add-port=8090/tcp --permanent
sudo firewall-cmd --reload
```

### 7. 连接 Claude.ai

1. 打开 [Claude.ai](https://claude.ai)
2.以此点击头像 -> **Settings** -> **Connectors**
3. 点击 **Add custom connector**
4. 填入 URL: `http://你的公网IP:8090/mcp`
5. 成功连接后，在对话中即可使用 `@Douyin Knowledge` 搜索你的视频笔记库。

## 服务管理

```bash
sudo systemctl start/stop/restart douyin-bot
journalctl -u douyin-bot -f   # 实时日志
```

## 常见问题

| 问题 | 解决方案 |
|------|----------|
| 视频解析失败 | 检查网络，部分地区需要代理 |
| PDF 中文乱码 | 确认已安装 `google-noto-sans-cjk-ttc-fonts` |
| 内存不足 | 建议服务器至少 1GB RAM |
| 消息收不到 | 检查企业微信应用可见范围 |
