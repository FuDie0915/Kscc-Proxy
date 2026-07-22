# KSCC 本地中转代理 - 使用文档

一个独立运行的本地中转服务。KSCC(金山云)LLM 后端带客户端校验,只能用 kscc 官方客户端访问;本程序在本机起一个中转,用 KSCC 的特殊认证去访问后端,然后对外暴露标准接口,让任何兼容 OpenAI 或 Anthropic 的客户端指向 `http://localhost:port` 就能用上 KSCC。

- **三协议对外**:同时暴露 OpenAI Chat Completions `/v1/chat/completions`、OpenAI Responses `/v1/responses`、Anthropic `/v1/messages`(均含 SSE 流式)。
- **配置从 JSON 文件读取**。

---

## 目录

1. [快速开始](#1-快速开始)
2. [配置说明](#2-配置说明)
3. [运行参数](#3-运行参数)
4. [接客户端](#4-接客户端)
5. [接口说明](#5-接口说明)
6. [故障排查](#6-故障排查)
7. [文件结构](#7-文件结构)

---

## 1. 快速开始

### 1.1 安装依赖

```bash
cd d:\Project\kscc_proxy
pip install -r requirements.txt
```

依赖:anthropic、fastapi、uvicorn、pydantic、httpx。

### 1.2 填配置

编辑 [config/kscc_proxy.json](config/kscc_proxy.json),至少改这两项(也可不预填,直接 1.3 启动会交互式引导):

> 若从 git 克隆(真实配置被 `.gitignore` 忽略,仓库里只有示例),先复制:`copy config\kscc_proxy.example.json config\kscc_proxy.json`(bash 用 `cp`),再编辑。

```json
{
  "kscc_token": "你的KSCC令牌",
  "kscc_base_url": "https://api.kscc.xxx.com"
}
```

- `kscc_token`:KSCC 后端访问令牌(也可用环境变量 `KSCC_AUTH_TOKEN` 代替)。
- `kscc_base_url`:KSCC 后端根地址,**不要带 `/v1`**(程序自动拼成 `…/v1/messages?beta=true`)。

### 1.3 启动

> 配置未填(`config/kscc_proxy.json` 不存在,或 `kscc_token`/`kscc_base_url` 为空且无 `KSCC_AUTH_TOKEN` 环境变量)时,启动会**交互式引导**填写并自动写回 `config/kscc_proxy.json`:token 输入隐藏回显,`kscc_base_url` 自动去掉尾部 `/v1`。填好后即继续启动。

**方式 A:直接启动(首次推荐)**

```bash
cd d:\Project
python -m kscc_proxy --config kscc_proxy/config/kscc_proxy.json
```

**方式 B:用启动脚本**

| 脚本 | 适用 | 用法 |
|---|---|---|
| [start.bat](start.bat) | Windows,双击/命令行 | 双击,或 `start.bat --port 9000` |
| [start.sh](start.sh) | git-bash / WSL | `bash start.sh`,或 `./start.sh`(先 `chmod +x`) |

脚本会自动切到父目录 `d:\Project`,透传参数(如 `--host 0.0.0.0 --port 9000`)。首次运行前需装依赖:`pip install -r kscc_proxy/requirements.txt`。

看到下面这行就说明起来了:

```
INFO - kscc_proxy - starting kscc_proxy on 127.0.0.1:8787 -> backend https://api.kscc.xxx.com
```

### 1.4 自测

新开一个终端,发请求验证是否连通:

```bash
curl http://localhost:8787/v1/chat/completions \
  -H "Authorization: Bearer <你配的 auth.api_key>" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"glm-5.2\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

返回 JSON(含 `choices[0].message.content`)即成功;返回 502 多半是 token 或 base_url 填错。

---

## 2. 配置说明

配置文件 [kscc_proxy.json](config/kscc_proxy.json) 各字段:

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `kscc_token` | string | `""` | KSCC 后端访问令牌。为空时回退环境变量 `KSCC_AUTH_TOKEN`;两者都空则启动时交互引导。 |
| `kscc_base_url` | string | `""` | KSCC 后端根地址(不含 `/v1`)。必填,为空则启动时交互引导。 |
| `default_model` | string | `""` | 请求未带 `model` 字段时的回退值。 |
| `fallback_model` | string | `""` | 未命中 `model_map` 的 model 名回退到此值。**为空 = 未命中则原样透传**(兼容后端真实支持的 model);设为如 `glm-5.2` 则客户端发任何未映射的 model 都落到它(典型:ChatGPT 类客户端后台用 `gpt-4o-mini` 等小模型做标题/摘要请求,设此项可避免 403 `ModelForbidden`)。 |
| `model_map` | object | `{}` | 外部 model 名 → KSCC 实际 model 名映射。命中则替换,未命中透传(或回退 `fallback_model`)。 |
| `listen.host` | string | `"127.0.0.1"` | 监听地址。 |
| `listen.port` | int | `8787` | 监听端口。 |
| `auth.api_key` | string | `""` | 对外鉴权密钥。**为空 = 不鉴权**;非空则要求请求头 `Authorization: Bearer <api_key>` 匹配。 |
| `defaults.max_tokens` | int | `4096` | OpenAI 请求未带 `max_tokens` 时的回退值。 |
| `defaults.temperature` | float | `1.0` | OpenAI 请求未带 `temperature` 时的回退值。 |
| `logging.level` | string | `"INFO"` | 日志级别。 |
| `logging.file` | string | `""` | 日志文件路径。为空只输出到 stderr;非空同时写文件。 |

### 关于 token 的三种填法

- 写在配置文件 `kscc_token` 里(简单,但 token 落在磁盘上)。
- 配置留空,启动时**交互式输入**(隐藏回显,自动写回配置文件)。
- 配置留空,改用环境变量:

  ```bash
  set KSCC_AUTH_TOKEN=你的KSCC令牌
  python -m kscc_proxy --config kscc_proxy/config/kscc_proxy.json
  ```

  优先级:配置文件 `kscc_token` > 环境变量 `KSCC_AUTH_TOKEN`;两者都空时启动才交互引导(输入后写回配置文件)。

### 关于 model_map

把"客户端习惯用的 model 名"映射到"KSCC 实际支持的 model 名":

```json
"model_map": {
  "gpt-4o": "glm-5.2",
  "gpt-4": "glm-5.2"
}
```

客户端发 `gpt-4o` → 中转转成 `glm-5.2` 发给 KSCC。不在表里的 model 名原样透传。`model_map` 为空时全部透传。

`default_model` 与 `model_map` 的关系:请求没带 model → 用 `default_model` 回填 → 再过 `model_map` 映射。

`fallback_model`:请求带了 model 但**不在 `model_map` 里**时,若 `fallback_model` 非空就用它替换,否则原样透传。常用于"客户端发什么 model 都要落到同一后端 model"的场景——例如 ChatGPT 类客户端除了主对话(用你配置的 `gpt-4o`),还会在后台用 `gpt-4o-mini`/`gpt-4.1-mini` 等小模型发**生成标题、摘要**的请求;这些名字不在 `model_map` 里会原样透传给后端,被以 `ModelForbidden`(403)拒绝。把 `fallback_model` 设成 `glm-5.2` 即可让这些请求也走 glm-5.2。

---

## 3. 运行参数

```bash
python -m kscc_proxy --config <路径> --host <地址> --port <端口>
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--config` | 同目录 `config/kscc_proxy.json` | 配置文件路径。 |
| `--host` | 配置里的 `listen.host` | 覆盖监听地址。设 `0.0.0.0` 允许局域网访问。 |
| `--port` | 配置里的 `listen.port` | 覆盖监听端口。 |

示例:换端口、开局域网

```bash
python -m kscc_proxy --config kscc_proxy/config/kscc_proxy.json --host 0.0.0.0 --port 9000
```

---

## 4. 接客户端

> **鉴权说明**:[kscc_proxy.json](config/kscc_proxy.json) 默认 `auth.api_key` 为空 = **不鉴权**,本机自用时下方所有 `api_key` / `Authorization` 头**可省略,或填任意非空值**(代理不校验)。若你设置了 `auth.api_key`(如下面用 `mykey123` 举例),则客户端必须填**完全相同的值**,否则 401。两种情况按客户端类型二选一。
>
> **客户端强制要填 Key / 模型名时**(有些客户端这两项不能留空):Key 填 `sk-kscc-proxy`(任意非空值均可,代理不校验);模型名填 `gpt-4o`(`model_map` 会映射到 `glm-5.2`)或直接填 `glm-5.2`。客户端发什么 model 都会先经 `model_map` 映射,命中则替换、未命中透传给后端。

### 4.1 OpenAI 类客户端

适用于:ChatBox、NextChat、LobeChat、OpenWebUI、Cline、各类"自定义 OpenAI 接口"的客户端。

在客户端设置里填:

| 设置项 | 值 |
|---|---|
| API 地址 / Base URL | `http://localhost:8787/v1` |
| API Key | `mykey123` |
| 模型名 | `glm-5.2`(或 `model_map` 里映射的名字) |

注意 URL **带 `/v1`**。

> **Responses API 客户端**(部分 "ChatGPT" 类客户端、Codex、`openai` SDK 的 `responses.create`)默认请求 `/v1/responses` 而非 `/v1/chat/completions`。这类客户端用**同一个** `http://localhost:8787/v1` 即可,中转已实现 `/v1/responses`(见 [5.2](#52-openai-responses-兼容端点))。若这类客户端的 Key/模型名不能留空,按上面鉴权说明填 `sk-kscc-proxy` / `gpt-4o`。

### 4.2 Anthropic 类客户端

适用于:Claude Code、Cherry Studio 的 Claude 模式、Anthropic SDK 等。

设环境变量后启动客户端:

```bash
set ANTHROPIC_BASE_URL=http://localhost:8787
set ANTHROPIC_AUTH_TOKEN=mykey123
```

或在客户端设置里:

| 设置项 | 值 |
|---|---|
| Base URL | `http://localhost:8787` |
| 密钥 / Auth Token | `mykey123` |

注意 URL **不带 `/v1`**(Anthropic SDK 自己会加)。

### 4.3 代码调用

OpenAI SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8787/v1", api_key="mykey123")
r = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "你好"}],
)
print(r.choices[0].message.content)
```

Anthropic SDK:

```python
from anthropic import Anthropic
c = Anthropic(base_url="http://localhost:8787", auth_token="mykey123")
print(c.messages.create(
    model="glm-5.2", max_tokens=100,
    messages=[{"role": "user", "content": "你好"}],
).content[0].text)
```

---

## 5. 接口说明

### 5.1 OpenAI 兼容端点

**`POST /v1/chat/completions`**

- 非流式(`stream` 不传或为 `false`):返回标准 OpenAI `chat.completion` 结构,含 `choices[0].message.content`、`tool_calls`、`finish_reason`、`usage`。
- 流式(`stream: true`):返回 SSE,逐行 `data: {chunk}\n\n`,首个 chunk 带 `delta.role:"assistant"`,文本增量在 `delta.content`,工具调用增量在 `delta.tool_calls`,结束 chunk 带 `finish_reason`,最后 `data: [DONE]`。
- `stream_options.include_usage: true`:流末多发一个带 `usage` 的 chunk。

格式转换(程序自动处理,客户端无感):
- OpenAI 多条 `system` 消息 → Anthropic 顶层 `system`(合并拼接)。
- OpenAI `tool` 角色消息 → Anthropic `tool_result` block;连续多条合并进同一条 user 消息。
- OpenAI `tool_calls`(arguments 是 JSON 字符串)→ Anthropic `tool_use` block(arguments 解析为对象)。
- OpenAI 图片 `image_url`(data URL / 普通 URL)→ Anthropic image block。

### 5.2 OpenAI Responses 兼容端点

**`POST /v1/responses`**

OpenAI 新版 Responses API(部分 "ChatGPT" 类客户端、Codex、`openai` SDK 的 `responses.create` 默认走这里)。与 Chat Completions 形状不同,但中转自动转换:

请求侧:
- `input`(字符串或 input item 数组)+ `instructions` → Anthropic `messages` + `system`。
  - `input` 为字符串 → 一条 user 消息。
  - 数组里的 `message`(role=user/assistant)→ 对应消息;role=system/developer 合并进顶层 `system`。
  - `function_call` item → assistant 的 `tool_use`;`function_call_output` item → user 的 `tool_result`。
  - `reasoning` item 跳过。
- `tools`(function 类型,扁平 `name`/`parameters` 或嵌套 `function.*` 均可)→ Anthropic tools;内置工具(web_search / file_search 等)跳过。
- `tool_choice`(`auto`/`none`/`required`→`any`/`function`→`tool`)相应映射。
- `max_output_tokens` → Anthropic `max_tokens`;`temperature`/`top_p` 透传。
- `model` 经 `model_map` 映射(同 Chat Completions)。
- `previous_response_id` 等会话状态**不支持**(中转无状态,客户端需自行传完整 `input`)。

响应侧:
- 非流式:返回标准 Responses `response` 对象,`output` 为 `message`(`output_text` part)与 `function_call` item 的数组,含 `usage`(`input_tokens`/`output_tokens`/`total_tokens`)。
- 流式(`stream: true`):返回带 `event:` 前缀的 SSE,事件序列 `response.created` → `response.in_progress` → `response.output_item.added` → `response.content_part.added` → `response.output_text.delta`(文本增量)→ `response.output_text.done` → `response.content_part.done` → `response.output_item.done` → … → `response.completed`(收尾,**不发** `data: [DONE]`)。工具调用增量走 `response.function_call_arguments.delta` / `.done`。

### 5.3 Anthropic 兼容端点

**`POST /v1/messages`**

后端本身就是 Anthropic 格式,此端点**字节级透传**(最保真):

- 只覆盖 `model` 字段(经 `model_map` 映射)。
- 其余字段(`system` / `messages` / `tools` / `tool_choice` / `thinking` / `temperature` / `top_p` / `stop_sequences` 等)原样透传。
- 客户端的 `anthropic-version`、`anthropic-beta` 等功能性请求头一并透传给后端(认证头除外,由中转固定头覆盖)。
- 非流式:原样返回后端 Anthropic 响应(`type:"message"`、`content`、`stop_reason`、`usage`)。
- 流式:原样透传后端 SSE(`event: message_start` / `content_block_delta` / `message_delta` / `message_stop`)。
- 客户端**不需要**传 `?beta=true`,`beta` 由中转注入后端。

### 5.4 健康检查

**`GET /healthz`(或 `/healthz/`)** → `{"status":"ok"}`(免鉴权)。

### 5.5 鉴权

当 `auth.api_key` 非空时,除 `/healthz` 外所有请求都要求:

- 请求头 `Authorization: Bearer <api_key>`,或裸 `Authorization: <api_key>`,或 `x-api-key: <api_key>`。
- 不匹配返回 401。

`auth.api_key` 为空时,任意请求放行(适合本机自用)。

---

## 6. 故障排查

| 现象 | 可能原因与排查 |
|---|---|
| 启动交互式引导填 token/base_url | `config/kscc_proxy.json` 不存在,或 `kscc_token`/`kscc_base_url` 为空且无 `KSCC_AUTH_TOKEN` 环境变量。填好自动写回再启动。 |
| 启动报"配置文件格式错误" | JSON 语法错误,检查逗号/引号(此情况不交互,需手动修)。 |
| 客户端报 401 | `auth.api_key` 已设,但客户端没填或填错 key。 |
| 客户端报 400 "invalid request" | 请求体不是合法 JSON,或 OpenAI 请求体缺少必填的 `messages`。 |
| 客户端报 502 | 中转连不上 KSCC 后端。检查 `kscc_token`、`kscc_base_url` 是否正确(常见:base_url 多写了 `/v1`)。 |
| 流式不显示 | 客户端要求数据实时推送;中转已设 `Cache-Control: no-cache`、`X-Accel-Buffering: no`,若经过 nginx 等反向代理,确认代理未缓冲。 |
| 客户端报 502 / 403 `ModelForbidden`「模型限制」 | 后端拒绝了代理转发的 model 名。看日志 502 行的 `model=...`:若不是你期望的(如显示 `gpt-4o-mini`),说明客户端发了一个**不在 `model_map` 的 model** 被原样透传了 → 设 `fallback_model: "glm-5.2"` 让它回退;若 `model=` 已是正确后端 model 仍被拒,则是账号/后端对该 model 的权限问题,需联系管理员。 |
| OpenAI 客户端连不上但 Anthropic 能连 | OpenAI 客户端 Base URL 应**带 `/v1`**(`http://localhost:8787/v1`);Anthropic 客户端**不带**(`http://localhost:8787`)。 |
| "ChatGPT"/Responses 类客户端报 404 `/v1/responses` | 这类客户端走 OpenAI Responses API(而非 Chat Completions),中转已支持 `/v1/responses`(见 [5.2](#52-openai-responses-兼容端点))。确认 Base URL 带 `/v1`;Key/模型名不能留空就填 `sk-kscc-proxy` / `gpt-4o`。 |
| 工具调用重复 / 报错 | 程序已处理"有 partial_json 逐片"与"仅在 stop 有完整 input"两种后端行为且不重复发送;若仍异常,反馈客户端名称与日志。 |

### 查日志

日志一行一条,格式 `HH:MM:SS LEVEL 消息`,终端彩色(不支持彩色的终端自动无色),重定向到文件无 ANSI 码。`logging.level` 设 `DEBUG` 可看更多;`logging.file` 设一个路径则同时写文件。默认只输出到启动终端的 stderr。

### 人工验证连通性

> 以下假设已设 `auth.api_key=mykey123`。**未设鉴权时**,删掉 `-H "Authorization: Bearer mykey123"` 即可。

```bash
# OpenAI 非流式
curl http://localhost:8787/v1/chat/completions \
  -H "Authorization: Bearer mykey123" -H "Content-Type: application/json" \
  -d "{\"model\":\"glm-5.2\",\"messages\":[{\"role\":\"user\",\"content\":\"说你好\"}]}"

# OpenAI 流式
curl -N http://localhost:8787/v1/chat/completions \
  -H "Authorization: Bearer mykey123" -H "Content-Type: application/json" \
  -d "{\"model\":\"glm-5.2\",\"messages\":[{\"role\":\"user\",\"content\":\"数到3\"}],\"stream\":true,\"stream_options\":{\"include_usage\":true}}"

# Anthropic 非流式
curl http://localhost:8787/v1/messages \
  -H "Authorization: Bearer mykey123" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
  -d "{\"model\":\"glm-5.2\",\"max_tokens\":100,\"messages\":[{\"role\":\"user\",\"content\":\"说你好\"}]}"

# Anthropic 流式
curl -N http://localhost:8787/v1/messages \
  -H "Authorization: Bearer mykey123" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
  -d "{\"model\":\"glm-5.2\",\"max_tokens\":100,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"数到3\"}]}"
```

---

## 7. 文件结构

```
kscc_proxy/                     # 包根(包名即 kscc_proxy,python -m kscc_proxy 入口)
├── __init__.py                 # 包标记
├── __main__.py                 # 入口:python -m kscc_proxy
├── app.py                      # FastAPI 组装 + 鉴权 + /healthz
├── core/                       # 基础设施
│   ├── config.py               #   配置加载 + model 映射 + 交互引导
│   ├── kscc_backend.py         #   KSCC 认证:anthropic SDK + httpx 双客户端
│   ├── models.py               #   OpenAI 请求体模型
│   ├── sse.py                  #   SSE 封装
│   └── logging_setup.py        #   统一日志(彩色 + 防方格)
├── api/                        # HTTP 端点 + 转换
│   ├── routes_openai.py        #   /v1/chat/completions
│   ├── routes_responses.py     #   /v1/responses(OpenAI Responses API)
│   ├── routes_anthropic.py     #   /v1/messages(httpx 透传 + 头透传)
│   ├── convert_openai.py      #   Chat Completions ↔Anthropic 格式转换 + 流事件→SSE 映射
│   └── convert_responses.py    #   Responses ↔Anthropic 格式转换 + response.* 流事件映射
├── config/                     # 配置文件目录
│   ├── kscc_proxy.json         #   真实配置(含 token,被 .gitignore 忽略)
│   └── kscc_proxy.example.json #   示例配置(空 token,提交到仓库)
├── scripts/                    # 启动脚本
│   ├── start.bat               #   Windows
│   └── start.sh                #   git-bash/WSL
├── .gitignore
├── requirements.txt            # 独立依赖
└── README.md                   # 本文档
```

### 数据流

```
别的客户端 ──(OpenAI / Responses / Anthropic 协议)──> 本中转(localhost:8787)
                                          │
                          ┌───────────────┼───────────────┐
                          │               │               │
              Chat Completions 端点   Responses 端点   Anthropic 端点
                  (格式转换)          (格式转换)        (字节透传)
                          │               │               │
                          └───────────────┴───────────────┘
                                      │  Authorization: Bearer + KSCC 固定头 + ?beta=true
                                      ▼
                                  KSCC 后端
```
