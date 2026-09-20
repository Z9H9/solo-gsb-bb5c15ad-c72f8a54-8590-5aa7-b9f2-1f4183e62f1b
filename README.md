# 文本发布前脱敏 API

基于 FastAPI 的文本脱敏服务：客户端提交原文与多条正则脱敏规则（含优先级、替换模板），
服务找出全部命中，按 **优先级 → 起始位置 → 规则顺序** 确定性消解重叠，仅替换胜出区间，
返回脱敏文本、每个命中的处理状态及原文/结果区间映射。非法正则逐条报错，不影响其他规则。
纯内存处理，无数据库。

## 快速开始（单条命令）

```bash
docker compose up --build
```

启动后访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/api/v1/desensitize | 脱敏接口（POST） |
| http://localhost:8000/health | 健康检查（GET） |
| http://localhost:8000/docs | Swagger 交互文档 |
| http://localhost:8000/ | 服务信息 |

服务默认监听 **8000** 端口（容器内外一致）。改宿主机端口：`API_PORT=9000 docker compose up --build`。

不使用 Compose 时也可直接构建运行：

```bash
docker build -t text-desensitize-api .
docker run -d -p 8000:8000 text-desensitize-api
```

## 配置（均为环境变量，可选）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_PORT` | `8000` | 宿主机映射端口（仅 Compose 使用） |
| `MAX_TEXT_LENGTH` | `1000000` | 原文最大字符数，超出返回 422 |
| `MAX_RULES` | `500` | 单次请求最大规则数，超出返回 422 |

示例：`docker run -e MAX_TEXT_LENGTH=500000 -p 8000:8000 text-desensitize-api`

## API 说明

### POST /api/v1/desensitize

请求体：

```json
{
  "text": "张三手机13800001111，邮箱 zhangsan@example.com",
  "rules": [
    {"id": "phone", "pattern": "1\\d{10}", "priority": 10, "replacement": "[手机号]"},
    {"id": "email", "pattern": "(\\w+)@[\\w.]+", "priority": 5, "replacement": "\\1@***"},
    {"id": "bad",   "pattern": "(",         "priority": 0,  "replacement": "Y"}
  ]
}
```

字段说明：

- `id`（可选）：客户端自定义规则标识，响应中原样返回，便于对账；
- `pattern`（必填）：Python `re` 语法正则；
- `priority`（可选，默认 0）：优先级，**数值越大越优先**；
- `replacement`（必填）：替换模板，支持 `\1`、`\g<name>` 分组引用；空字符串表示删除命中内容。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/api/v1/desensitize \
  -H 'Content-Type: application/json' \
  -d '{"text":"手机13800001111","rules":[{"pattern":"1\\d{10}","priority":1,"replacement":"[手机号]"}]}'
```

响应（节选）：

```json
{
  "masked_text": "张三手机[手机号]，邮箱 zhangsan@***",
  "matches": [
    {
      "rule_index": 0, "rule_id": "phone", "status": "applied",
      "original_span": {"start": 4, "end": 15},
      "result_span":   {"start": 4, "end": 9},
      "matched_text": "13800001111", "replacement": "[手机号]",
      "reason": null, "overlapped_by": null
    }
  ],
  "rule_errors": [
    {"rule_index": 2, "rule_id": "bad", "pattern": "(", "stage": "compile",
     "error": "missing ), unterminated subpattern at position 0"}
  ],
  "stats": {"text_length": 34, "masked_length": 26, "rules_total": 3,
            "rules_invalid": 1, "matches_found": 2,
            "matches_applied": 2, "matches_rejected": 0}
}
```

- `matches`：全部命中（按原文区间排序）。`status` 为 `applied`（已替换）或
  `rejected_overlap`（因与胜出区间重叠被否决，附 `reason` 与 `overlapped_by`）。
- `original_span` / `result_span`：命中在原文/结果中的区间（`[start, end)`，按字符计），
  即原文与结果的区间映射。被否决命中的结果区间指向其在结果中存活的片段；
  若内容被胜出区间完全覆盖，则结果区间为空区间（`start == end`），位于替换内容之后。
- `rule_errors`：非法规则逐条报错（`stage` 区分正则编译失败 `compile` 与替换模板非法
  `replacement`），不影响其他规则执行。

### 重叠消解规则（确定性）

1. `priority` 数值大者优先；
2. 优先级相同，命中起始位置靠前者优先；
3. 起始位置也相同，规则在请求 `rules` 中出现顺序靠前者优先；
4. 以上全相同则以区间更长者、全局发现顺序兜底，保证结果完全确定。

按此顺序贪心选出互不重叠的胜出区间，仅对胜出区间执行替换；零宽命中忽略。

### GET /health

返回 `{"status": "ok"}`，用于存活探针（镜像内置 `HEALTHCHECK` 亦使用该端点）。

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000   # 启动服务
pytest                                      # 运行测试（核心逻辑 + API 端到端）
```

核心脱敏逻辑在 `app/masking.py`，不依赖第三方包，可单独测试：
`python3 -m unittest discover -s tests -p "test_masking.py"`。

## 项目结构

```
app/
  main.py       # FastAPI 入口：/health、/api/v1/desensitize
  masking.py    # 核心逻辑：命中收集、重叠消解、区间映射（纯标准库）
  models.py     # 请求/响应模型（Pydantic）
  config.py     # 环境变量配置
tests/          # 核心逻辑与 API 测试
Dockerfile      # 依赖安装与启动初始化均纳入镜像
docker-compose.yml
requirements.txt / requirements-dev.txt
```
