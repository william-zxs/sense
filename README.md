# 家庭 Wi-Fi 连接感知服务

这是一个轻量服务器，用来观察家里成员设备是否“在线”（连接在家庭局域网/Wi-Fi 上）。

## 功能

- 通过 `ip neigh show`（ARP/NDP 邻居表）检测设备是否在线。
- 可选 `arp-scan --localnet` 主动扫描（适合邻居表不完整的环境）。
- 记录“上线/离线”事件。
- 提供 HTTP API：
  - `GET /health`
  - `GET /status`
  - `GET /events?limit=100`

## 快速开始

1. 安装依赖：

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. 配置设备：

   ```bash
   cp config.example.yaml config.yaml
   # 编辑 config.yaml，填入每个人手机的真实 MAC 地址
   ```

3. 启动服务：

   ```bash
   uvicorn server:app --host 0.0.0.0 --port 8000
   ```

4. 查询状态：

   ```bash
   curl http://127.0.0.1:8000/status
   curl http://127.0.0.1:8000/events?limit=20
   ```

## 如何拿到手机 MAC 地址

- **iPhone**：设置 → 通用 → 关于本机 → Wi‑Fi 地址。
- **Android**：设置 → 关于手机/关于设备 → 状态信息 → Wi‑Fi MAC 地址（不同品牌路径略有差异）。

## 注意事项

- iOS/Android 可能对不同 Wi‑Fi 使用“随机 MAC”（私有地址）。请确保你填入的是当前家庭 Wi‑Fi 实际使用的地址。
- 如果设备在待机省电状态，邻居表可能变慢更新，状态会有少量延迟。
- 建议把服务部署在路由器旁边的常开设备（树莓派/NAS/小主机）上。
