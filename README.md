# 双机 → 单机改造总结（最终版）

> 目标：同一台 Windows 电脑既运行 PC 版游戏，又运行解析工具完成 TGCP 中间人、
> UDP 解密与网页雷达，不再需要「开热点 + 第二台设备」。
>
> 状态：**已实现、已联调、已上线**。
>
> 备份提交：`c0c510d`（改造前的源码快照）；本次单机模式与卡密改动尚未提交。

---

## 1. 最终效果

以管理员身份运行 exe 后：

```
1. 要求输入卡密（每次启动都要求）
2. 卡密校验通过（/verify；未激活的卡自动 /redeem）
3. 选择模式（直接回车 = 单机模式）
4. 本机 layer 0 拦截游戏 TCP → TGCP 中间人 → 提取 bank
5. 旁路观察 UDP → 解密下行
6. 解析器还原 UE 包 → 浏览器打开 2D/3D 雷达
```

启动菜单：

```
[1] 双机模式：手机 / 第二台电脑连接本机热点
[2] 单机模式：本机运行游戏，自动识别 TGCP
[Q] 退出
请选择（直接回车 = 单机模式）:
```

---

## 2. 原双机架构

```
手机 / 第二台电脑（游戏客户端）
        │  Wi-Fi
        ▼
PC（Windows 移动热点）
  ├─ WinDivert layer 1（FORWARD）拦截手机 TCP
  ├─ WinDivert layer 1（SNIFF）旁路观察手机 UDP
  ├─ 改写到本机监听端口 → TcpRelayServer
  │    └─ TgcpSession：DH 中间人 + AES 解密/重加密
  ├─ UdpKeyBroker：用大厅下发的 128 字节 bank 解密 UDP 下行
  └─ NvaParser：解析 UE 包 → Web 雷达
```

特点：

- 工具运行在热点 AP 上；
- 过滤器按**客户端 IP**（手机）匹配，不需要服务器 IP/端口；
- 本机流量不经过 `FORWARD` 层。

---

## 3. 单机的技术障碍与验证

### 3.1 三个障碍

1. 本机流量走 `NETWORK` 层，不经过现有的 `FORWARD` 句柄；
2. 启动强制要求 `Wi-Fi Direct` 热点网卡和热点子网；
3. 中继与游戏同机时，回程走本地回环，需要确认能否拦截。

### 3.2 验证

- Wireshark 确认 PC 版同样使用 TGCP 魔数 `33 66 00 0B 00 0C`；
- `--probe-loopback` 实测 layer 0 能看到本地回环连接（`127.0.0.1` 与 LAN IP 均捕获到包）；
- `--probe-local <IP> <端口>` 确认 layer 0 能看到游戏的真实 TCP/UDP；
- 之后才进入实现。

---

## 4. 单机模式设计

### 4.1 两种模式

| 模式 | 命令 | 过滤器 | 适用场景 |
|---|---|---|---|
| 固定端点 | `--local <服务器IP> <端口>` | 只匹配指定 `IP:端口` | 地址稳定，开销最小 |
| 自动发现 | `--local-auto` | 本机所有新 TCP | 服务器轮换，无需手填地址 |

自动发现依赖中继已有的前缀嗅探：

- TGCP 前缀 → DH 中间人；
- 非 TGCP → 原样直通（`session == null` 的 passthrough 分支）。

### 4.2 数据流

```
本机游戏
   │
   ├─ TCP（登录/大厅）
   │    → WinDivert layer 0 拦截 outbound TCP
   │    → 改写：src=本机:别名端口, dst=本机:监听端口
   │    → TcpRelayServer 接受连接
   │         ├─ Resolve(本机:别名端口) → 真实服务器
   │         ├─ 上游 socket 绑定本机 IP（端口登记到排除表）
   │         └─ TgcpSession 解密/重加密
   │    → 回程：监听端口的包改写回 服务器IP:端口 → 游戏
   │
   └─ UDP（对局）
        → WinDivert layer 0 SNIFF（不改包）
        → UdpKeyBroker 观察 + 绑定 bank + 解密下行
```

### 4.3 关键难点与解决

| 难点 | 解决 |
|---|---|
| 本机 TCP 不走 FORWARD | 新开 layer 0 句柄 |
| 客户端和网关是同一个 IP | 20000–59999 别名端口区分流 |
| 中继自己的上游连接会被再次拦截 | 上游 socket 先绑定并上报端口，路由器按源端口排除 |
| 已存在的连接被误伤 | 只在 SYN 上建立路由，其他包原样放行 |
| 自动模式会匹配到中继回给游戏的包 | 正向过滤器加 `tcp.SrcPort != 监听端口` |
| 自动模式拦截本地服务/组播 | 用户态跳过回环、组播、同 /24 地址 |
| 非 TGCP 长连接被 60 秒空闲断开 | 自动模式空闲超时放宽到 600 秒 |
| UDP broker 流上限异常 | UDP 回调加 try/catch，避免带走观察线程 |

---

## 5. 代码改动清单

### 5.1 新增

| 文件 | 作用 |
|---|---|
| `Realtime/LocalCaptureRouter.cs` | 本机 layer 0 路由器：TCP 拦截/改写/回程、UDP 旁路、固定端点与自动发现两种过滤器 |
| `源码/单机模式改造总结.md` | 本文档 |

### 5.2 修改

| 文件 | 改动 |
|---|---|
| `Realtime/TcpRelayServer.cs` | 新增 `upstreamBind`（上游绑定本机 IP）、`OnUpstreamBound`（连接前上报端口供排除）、`idleTimeoutMs`（自动模式 600 秒） |
| `Realtime/RealtimeSession.cs` | 新增 `_localRouter`、`StartLocalAsync`、快照/释放适配；本机模式不要求热点 |
| `Program.cs` | 新增 `--probe-local`、`--probe-loopback`、`--local`、`--local-auto`、`--license-info`；本机 IP 自动探测 |
| `GatewayConsoleApplication.cs` | 启动模式菜单（回车默认单机）；单机模式运行与停止；单实例锁复用 |
| `License/LicenseService.cs` | 每次启动要求卡密；`/verify` 失败未激活时自动 `/redeem`；服务器地址校验；稳定设备指纹；旧授权迁移；`--license-info` |
| `License/LicenseService.cs` 设备指纹 | 由「机器名 + SID + 完整 OSVersion」改为「MachineGuid + SID + 机器名」 |

### 5.3 新增命令

| 命令 | 说明 |
|---|---|
| `--probe-local <IP> <端口> [秒数]` | 只读观察本机到指定服务器的 TCP/UDP |
| `--probe-loopback [秒数]` | 验证 layer 0 能否看到本地回环 |
| `--local <服务器IP> <端口> [本机IP] [UDP IP] [UDP端口]` | 固定端点单机模式 |
| `--local-auto [本机IP]` | 自动发现单机模式 |
| `--license-info` | 打印设备 ID、授权字段、签名匹配结果 |

---

## 6. 卡密与设备授权

### 6.1 每次启动都要求卡密

- 不再静默复用本地授权；
- 已激活的卡走 `/verify`（幂等，不消耗）；
- 未激活的卡在 `/verify` 返回「尚未激活」后自动走 `/redeem`；
- 过期/作废/不存在 → 提示原因并退出码 3；
- 本地授权文件仍然保存，但只用于**断网 72 小时离线宽限**和卡号比对。

### 6.2 设备指纹

```
device_id = SHA256(MachineGuid | 用户SID | 机器名 | 固定盐)[:32]
```

- `MachineGuid` 来自 `HKLM\SOFTWARE\Microsoft\Cryptography`；
- **不再包含完整 `Environment.OSVersion`**：它受 manifest 兼容性声明和 Windows 更新影响，
  同一台机器升级系统后会变，导致授权失效；
- 旧授权文件迁移：当前设备签名不匹配时，若文件自身签名自洽（用文件里的 `device_id` 能验证通过）先接受；
  下一次在线 `/verify` 成功时写回新的 `device_id` 和签名，无需重新买卡。

### 6.3 单实例锁

- 交互式主流程与 `--local` / `--local-auto` 共用 `%LocalAppData%\NvaLocalParser\interactive.lock`；
- 避免同时启动两个采集/中继实例。

---

## 7. 服务器部署

### 7.1 卡密后台

已部署在：

```
http://8.134.196.241:28473/
```

- FastAPI + SQLite，`uvicorn --host 0.0.0.0 --port 28473 --workers 1`
- 管理后台：浏览器打开根路径，输入 `ADMIN_API_KEY`
- 客户端 `license.json`：

```json
{ "server": "http://8.134.196.241:28473" }
```

- 卡密为时长卡，从激活时刻起算；支持固定端点、撤销、CSV 导出。

### 7.2 雷达网页挂到服务器

雷达页由本机解析器提供（`127.0.0.1:17891`），**不能直接拷静态文件**，需要隧道：

1. 服务器 `/etc/ssh/sshd_config`：

   ```
   GatewayPorts clientspecified
   ```

   ```bash
   sudo systemctl restart sshd
   ```

2. 本机（运行工具的电脑）：

   ```powershell
   ssh -R 0.0.0.0:28474:127.0.0.1:17891 root@8.134.196.241
   ```

3. 服务器确认监听：

   ```bash
   ss -ltnp | grep 28474
   # LISTEN 0 128 0.0.0.0:28474 0.0.0.0:*
   ```

4. 阿里云安全组放行 TCP 28474（服务器内部 `firewall-cmd` 也要放行）。

5. 访问：

   ```
   http://8.134.196.241:28474/
   ```

注意：

- 本机和工具必须一直运行；服务器只做转发；
- 雷达页**没有登录鉴权**，公网暴露后拿到地址的人都能看；
- 建议用随机高位端口、限制安全组来源，或用完就断开隧道；
- 长期常驻建议 `frp` / `autossh`。

---

## 8. 验证结果

| 项目 | 结果 |
|---|---|
| `--self-test-v5` | 通过，410 checks |
| `--probe-loopback` | `127.0.0.1` 与 LAN IP 均能看到 7 个包 |
| `--local-auto` 空跑 | 正常启动解析器、网页和 layer 0 路由 |
| PC 版游戏实机联调 | **通过**，自动发现 TGCP 并完成解密 |
| 卡密迁移 | 旧授权自动迁移，`sig_match: True` |
| 每次启动卡密 | 实测要求输入卡密后才进入模式菜单 |
| 服务器隧道 | `127.0.0.1:28474` 返回 200；公网入口已打通 |

---

## 9. 限制与注意事项

1. **只支持 IPv4**；游戏若走 IPv6 不会命中过滤器。
2. **必须重启游戏**：路由只在 SYN 上建立，工具启动前的连接会原样放行。
3. **自动模式会代理本机所有新 TCP**：
   - 非 TGCP 由中继原样直通；
   - 浏览器、更新程序会多一次用户态中转；
   - 自动模式空闲超时 600 秒；
   - 固定端点模式开销最小，地址稳定时优先用。
4. **固定端点模式只拦一个服务器**，服务器轮换时改用 `--local-auto`。
5. **UDP 观察范围**：
   - 固定端点：指定服务器 IP；
   - 自动模式：本机所有非本地 UDP；
   - 对局 UDP 服务器与 TCP 不同时，固定端点模式需补 UDP 地址。
6. **卡密**：每次启动校验；`--probe-*`、`--self-test`、`--verify-v5`、`--extract` 等开发命令不校验。
7. **检测面**：仍是「两段 TCP + 用户态中继」，客户端能看到 TCP RTT、TTL、TCP 选项等变化；
   本改造不涉及这些特征的隐藏。
8. **Git**：改造前备份为 `c0c510d`；单机模式与卡密改动尚未提交。

---

## 10. 后续可做

- [x] 单机 layer 0 采集与 TCP 中间人
- [x] 自动发现 TGCP 端点
- [x] 卡密校验接入单机模式，且每次启动要求卡密
- [x] 设备指纹改为 `MachineGuid`，旧授权自动迁移
- [x] 启动模式菜单（回车默认单机）
- [x] 服务器反向隧道访问雷达网页
- [ ] 自动模式增加「只在发现阶段代理全部 TCP，之后只代理已锁定端点」以降低开销
- [ ] 固定端点模式支持多个服务器端点
- [ ] 自动识别对局 UDP 服务器
- [ ] 雷达网页增加登录鉴权
- [ ] 将本次改动提交 Git 并推送
- [x] 更新 `使用说明.txt`，补充反向代理/SSH 隧道操作、卡密与网页雷达的区别
- [ ] 在使用说明中补充单机模式启动方式（目前以模式菜单呈现）
- [ ] usb线传输map—>phone

---

## 11. 快速命令

```powershell
# 正常启动（管理员）：先输卡密，再选模式（回车 = 单机）
.\单双机TQ.exe

# 直接命令行进入单机自动发现
.\Nva本地离线解析.exe --local-auto

# 固定端点
.\Nva本地离线解析.exe --local 112.86.231.222 6160

# 只读探针
.\Nva本地离线解析.exe --probe-local 112.86.231.222 6160 60
.\Nva本地离线解析.exe --probe-loopback 8

# 授权诊断
.\Nva本地离线解析.exe --license-info
```

```bash
# 服务器：卡密后台
curl http://8.134.196.241:28473/health
# 服务器：雷达隧道
ss -ltnp | grep 28474
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:28474/
```
