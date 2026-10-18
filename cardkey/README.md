# 卡密系统

Python + FastAPI 实现的卡密生成 / 分发 / 核销服务。

```
cardkey/
├── app/
│   ├── config.py      # 配置（.env 覆盖）
│   ├── database.py    # 引擎 / 会话
│   ├── models.py      # Batch / Card / RedeemLog
│   ├── schemas.py     # 请求响应模型
│   ├── security.py    # 卡密生成、Fernet 加解密、管理端鉴权
│   ├── ratelimit.py   # 滑动窗口限流
│   ├── service.py     # ★ 核心：批次生成 + 原子核销
│   ├── main.py        # HTTP 接口 + 管理后台静态页面
│   └── web/           # 可视化管理后台（原生 HTML/CSS/JS，无构建）
├── manage.py          # 命令行工具
├── requirements.txt
└── .env               # 密钥（init 时自动生成，不要外传）
```

## 快速开始

```bash
cd F:\mouse\cardkey
python -m pip install -r requirements.txt

python manage.py init                      # 生成 .env + 建表，会打印 ADMIN_API_KEY
python manage.py gen -p vip -n 100         # 生成 100 张卡密
python manage.py serve                     # 起服务
```

- 管理后台：<http://127.0.0.1:8000/>（输入 `ADMIN_API_KEY` 登录）
- 接口文档：<http://127.0.0.1:8000/docs>（生产环境建议 `ENABLE_DOCS=false` 关闭）

## 接口

管理端所有接口都要带请求头 `X-Admin-Key: <ADMIN_API_KEY>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/` | **可视化管理后台**（输入管理密钥登录） |
| GET | `/assets/*` | 后台静态资源 |
| POST | `/redeem` | **核销**（客户端首次激活调用，带限流） |
| POST | `/verify` | **校验**（客户端每次启动调用，幂等，带限流） |
| GET | `/admin/products` | 产品维度统计 |
| POST | `/admin/batches` | 生成批次，返回明文卡密（仅此一次） |
| GET | `/admin/batches` | 批次列表 |
| GET | `/admin/batches/{batch_no}/stats` | 单批次统计 |
| GET | `/admin/batches/{batch_no}/export` | 导出整批 CSV（`include_pwd=true` 含密码） |
| GET | `/admin/cards` | 卡密列表（卡号打码，支持 `card_no` 精确搜索） |
| POST | `/admin/cards/{card_no}/reveal` | 解密返回明文（补发用，重点审计） |
| POST | `/admin/cards/by-id/{id}/reveal` | 同上，按后台列表 id 操作 |
| POST | `/admin/cards/by-id/{id}/disable` | 作废未使用的卡 |
| POST | `/admin/cards/by-id/{id}/revoke` | 撤销卡（已激活的也立即失效） |
| POST | `/admin/cards/{card_no}/disable` | 作废未使用的卡 |
| POST | `/admin/cards/{card_no}/revoke` | 撤销卡（已激活的也立即失效） |
| GET | `/admin/logs` | 核销日志 |
| GET | `/admin/stats` | 全库统计 |

### 核销示例

```bash
curl -X POST http://127.0.0.1:8000/redeem \
  -H "Content-Type: application/json" \
  -d '{"card_no":"ABCD-EFGH-JKMN-PQRS-TUVW","device_id":"machine-001"}'
```

```json
{"code": 0, "msg": "核销成功", "data": {"product": "vip", "used_at": "2026-09-28T13:05:11"}}
```

失败时 `code` 非 0（`1001` 核销失败、`1002` 校验失败、`4290` 触发限流），
HTTP 状态码 200 / 429。客户端**只认 `code == 0`** 即可。

### 启动校验示例

```bash
curl -X POST http://127.0.0.1:8000/verify \
  -H "Content-Type: application/json" \
  -d '{"card_no":"ABCD-EFGH-JKMN-PQRS-TUVW","device_id":"machine-001"}'
```

```json
{"code":0,"msg":"ok","data":{"product":"vip","expires_at":1760000000,"used_at":1750000000,"server_time":1750000000}}
```

`expires_at` / `used_at` / `server_time` 都是 **Unix 秒**，不用 ISO 字符串，避免客户端时区解析出错。
`expires_at` 为 `null` 表示永久卡。

## 管理后台

浏览器打开 `http://127.0.0.1:8000/`（或部署后的 `https://license.你的域名/`），输入 `.env` 里的
`ADMIN_API_KEY` 登录。

- **概览**：卡密总数、未使用/已使用/作废/过期、7 天内到期、核销/校验失败次数
- **批次**：批次列表与状态计数、生成新批次、下载整批 CSV（可选含密码）
- **卡密**：按产品/批次/状态筛选、卡号精确搜索（支持小写与空格）、查看明文、作废、撤销
- **日志**：核销与校验日志，可按结果和 `verify:` 前缀筛选
- **设置**：浅色/深色主题、登出

安全说明：

- 管理密钥只保存在 `sessionStorage`，关闭标签页即失效；不写入 localStorage、不放进 URL。
- 页面使用严格 CSP，前端不使用内联脚本/样式；所有数据用 `textContent` 渲染。
- 明文卡密只在「生成结果」和「查看」对话框中出现，不写入浏览器存储。
- 生产环境必须 HTTPS；建议再在 Nginx 加 IP 白名单或 Basic Auth。

### 时长卡

```bash
python manage.py gen -p tq -n 100 --days 30     # 30 天时长卡，从激活时刻起算
python manage.py gen -p tq -n 100               # 永久卡（默认）
```

现有数据库升级：

```bash
python manage.py migrate
```

### 生成批次示例

```bash
curl -X POST http://127.0.0.1:8000/admin/batches \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -d '{"product":"vip","count":10}'
```

大批量（上万张）建议用命令行，避免 JSON 响应过大：

```bash
python manage.py gen -p vip -n 10000 -o vip.csv
```

## 设计要点

### 1. 核销的原子性 —— 整个系统唯一必须做对的事

应用层写 `if card.status == 0: card.status = 1` 是错的：两个并发请求会同时通过判断，
同一张卡被核销两次。正确做法是把状态判断下推到 SQL 的 `WHERE` 里，让数据库裁决
（见 `app/service.py: redeem()`）：

```sql
UPDATE cards SET status = 1, used_at = ?, used_by = ?
WHERE card_no = ? AND status = 0;
-- 受影响行数为 1 才算抢到，为 0 说明已被别人用掉
```

InnoDB 和 SQLite 对这条语句都会加行级写锁，天然互斥。换成 MySQL/PostgreSQL 同样成立。

### 2. 卡密生成

- CSPRNG（`secrets`），**绝不**用自增 ID / 时间戳 / 可推导序列 —— 这是被刷库的直接原因
- 31 字符集（剔除了 `0/O`、`1/I/L` 等易混字符），默认 20 位 ≈ 99 bit 熵
- 唯一性靠内存 Set + 分块查库双重保证，10 万张不会发 10 万条 SELECT

### 3. 密钥与存储

- `card_no` 明文存 + 唯一索引（必须能查，无法哈希化）
- `card_pwd`（可选第二因子）用 Fernet 加密存储，后台可解密补发
- `ADMIN_API_KEY`、`CARD_FERNET_KEY` 都在 `.env` 里，已加入 `.gitignore`
- 管理端鉴权用 `hmac.compare_digest` 定长比较

> 如果你的业务不需要「后台补发卡密」，把 `card_pwd` 改成只存哈希、明文只展示一次，
> 安全性更高。

### 4. 限流

`/redeem` 默认 60 秒内同一 IP 最多 10 次（滑动窗口）。卡密查询接口必须限流，
否则卡密空间再大也架不住脚本扫。

**单进程内存实现**，用 `--workers >1` 或多实例部署时每进程各算各的，
额度会被放大。那种场景要换成 Redis 版本（`INCR` + `EXPIRE`）。

`TRUST_PROXY` 默认 `false`。只有在 Nginx 后面、且反代会覆写 `X-Forwarded-For`
时才打开，否则客户端可以伪造这个头绕过限流。

## 宝塔（BT Panel）部署

以独立子域名 `license.example.com` 为例：

1. 上传 `cardkey/` 到 `/www/wwwroot/license/`。
2. 宝塔 → **Python 项目** → 添加项目：
   - Python 版本 3.11+
   - 项目路径：`/www/wwwroot/license`
   - 启动方式：`uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1`
   - 安装依赖：`pip install -r requirements.txt`
3. 初始化（或复用已有 `.env`）：

   ```bash
   cd /www/wwwroot/license
   python manage.py init     # 已有 .env 时不会覆盖
   python manage.py migrate  # 已有库补列，幂等
   ```

4. 新建站点 `license.example.com`，开启 HTTPS，反向代理到 `http://127.0.0.1:8000`：

   ```nginx
   location / {
       proxy_pass http://127.0.0.1:8000;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $remote_addr;   # 覆盖，不要用 $proxy_add_x_forwarded_for
       proxy_set_header X-Forwarded-Proto $scheme;
   }
   ```

5. `.env` 设置：

   ```
   TRUST_PROXY=true
   ENABLE_DOCS=false     # 生产关闭 /docs、/openapi.json
   ```

6. 防火墙只开 443；8000 只监听 `127.0.0.1`，不要对公网开放。
7. 自检：

   ```bash
   curl -s https://license.example.com/health
   python manage.py gen -p tq -n 10 --days 30
   curl -s -X POST https://license.example.com/redeem \
        -H 'Content-Type: application/json' \
        -d '{"card_no":"...","device_id":"test"}'
   curl -s -X POST https://license.example.com/verify \
        -H 'Content-Type: application/json' -d '{"card_no":"..."}'
   ```

8. 浏览器打开 `https://license.example.com/`，输入 `ADMIN_API_KEY` 进入管理后台。
9. 定期备份 `cardkey.db`（以及 `-wal` / `-shm`）。

## 上线前要做的

- [ ] 换 MySQL / PostgreSQL（`DATABASE_URL`），SQLite 不适合多实例
- [ ] 限流换 Redis，`workers` 才能开多
- [ ] 加 HTTPS，`ADMIN_API_KEY` 走内网或加 IP 白名单（管理后台同样受此保护）
- [ ] `.env` 里的密钥用 KMS / 环境变量注入，不要落在磁盘上
- [ ] 给 `/redeem` 和 `/admin/cards/*/reveal` 加访问审计
- [ ] 定期备份数据库 —— 卡密是钱，丢了就是事故

## 命令行参考

```
python manage.py init                       # 生成 .env + 建表
python manage.py migrate                    # 已有库补列（时长卡字段），幂等
python manage.py gen -p vip -n 100          # 生成并打印
python manage.py gen -p tq -n 100 --days 30 # 30 天时长卡
python manage.py gen -p vip -n 10000 -o a.csv
python manage.py gen -p vip -n 100 --with-pwd --pwd-length 12
python manage.py export -b B20260928-A1B2C3 -o out.csv
python manage.py stats                      # 全库统计
python manage.py batch B20260928-A1B2C3     # 单批次统计
python manage.py disable XXXX-XXXX-XXXX-XXXX-XXXX   # 作废未使用的卡
python manage.py revoke  XXXX-XXXX-XXXX-XXXX-XXXX   # 撤销（已激活的也失效）
python manage.py serve --host 127.0.0.1 --port 8000
```

  怎么用

  cd F:\mouse\cardkey
  python -m pip install -r requirements.txt
  python manage.py gen -p vip -n 100     # 生成卡密
  python manage.py serve                 # 起服务，文档在 http://127.0.0.1:8000/docs

  管理密钥已经在 F:\mouse\cardkey\.env 里（manage.py init 生成的），.env 和 *.db 都已加入 .gitignore。
