# ZKC42V e-paper DIY driver (macOS)

> 在 macOS 上通过 CoreBluetooth 直接驱动 Zkong Valley **ZKC42V** 4.2" 三色电子墨水价签（400x300 BWR）。
> 逆向官方协议后无需官方 App，直接用 Mac 推图、显示 AI 额度面板（含农历/节气/干支 + 道德经每日一句）。

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 特性

- 🔗 **纯 BLE 直连**：绕过官方云端/App，CoreBluetooth 直接推帧显示。
- 🖼 **传任意图片** `img`，及 `INIT(model)+SET_SLOT+WRITE_IMG(0x30,RLE)+REFRESH(0x05)` 协议。
- 📊 **AI 额度面板** `quotas`：codex / grok / kimi / opencode-go / Ollama Pro / Windsurf（Devin）六行实时额度 + 相对重置时间。
- 🎨 **Apple 风格排版**：白底 tile + 单一红色强调；头部大号时间 + 农历/节气/干支；底部随机《道德经》一句 + 白话（本地缓存，无需联网）。
- 🕘 **差量刷新 + 定时窗口**：数据没变自动跳过推送；`--start/--end` 限定活跃时段省电。
- 🔬 **逆向工具链**：`scan` / `inspect` / `cmd` / `seq` / `server`（模拟官方 App）。

## 快速开始

```bash
# 1) 构建 BLE 工具
swiftc -o build/bleprobe mac/BLEProbe.swift

# 2) 扫描并找到你的价签 UUID
./epaper.sh scan

# 3) 设好环境变量后推送任意图片
EPAPER_UUID=<你的UUID> ./epaper.sh img photo.png
```

**依赖**：macOS + Python 3（Pillow）；额度面板还用到 `lunar_python`（农历）：
```bash
pip3 install pillow lunar_python
```

## 目录

- `mac/BLEProbe.swift` — BLE 扫描 + GATT Profile 分析工具
  - `scan <seconds>`：扫描并列出附近 BLE 设备（含完整广播包 hex），保存 `data/scan-<ts>.json`
  - `inspect <UUID>`：连接指定设备，导出 Services / Characteristics / 可读属性，保存 `data/profile-<uuid>.json`
  - `cmd <UUID> <char> <hex>`：连接并写单条命令
  - `seq <UUID> <listen> <char:hex>...`：连接后顺序写入多条命令，监听响应
  - `send <UUID> <frame> [--init]`：连价签并按 EPD-nRF5 0x30 帧协议发送图像
  - `partial <UUID> <old-frame> <new-frame>`：实验性 SSD1619 窗口局部刷新（失败/不适合时整屏回退）
  - `server <MAC> [frame]`：模拟官方 ZkongESL App（GATT Server + 广播），价签主动连接
- `data/`：扫描与抓包结果

## 逆向结论（2026-08-08）

### 硬件
- 主控：GigaDevice **GR5513**，固件 `v1.10-gr5513`（读特征 `62750003` 得到）
- 屏幕：400x300 BWR（黑/白/红），SSD1619 类控制器

### 价签上的 GATT（价签 = server，广播服务 `62750001`）
- `62750002`：命令通道（write + notify），EPD-nRF5 风格命令；连接后回状态文本
  `slots=7 1 0`、`clock_enable=1`、`sid=...`、`mtu=244 rle=1`、`t=...`
- `62750003`：只读固件版本
- `A6ED0401`：数据服务（`A6ED0402` notify / `A6ED0403` writeNoResp / `A6ED0404` writeNoResp+indicate）

### 官方 App（ZkongESL 7.2.0，已反编译）的传图协议
**方向与直觉相反**：手机是 GATT Server，价签是 client。
1. App 调云端 `getDataByUuid` 拿到 Base64 图片数据 + 价签真实 MAC
2. 手机广播服务 `0783B03E-8535-B5A0-7140-A304D2495CB7`，厂商数据含价签 MAC
   （`[crc8, 0x06, 0x08, mac1..mac6]`，crc = XOR of {06,08,mac...}）
3. 价签扫描到自己的 MAC → 主动连接手机
4. MTU 协商后 `chunk = mtu - 20`（230B），手机按块 notify 到
   `0783B03E-...-5CB8`，价签 write ≥35B 回 ACK 流控，发完 <35B 确认

### 结论 / 状态（✅ 已打通）

**成功：Mac 直接驱动价签显示图片 + 时钟！**

关键步骤（来自 epdiy.cn 官方 Web 客户端逆向）：
1. `INIT(0x01, model=0x02)` — 初始化 SSD1619 三色面板（必须带模型参数！）
2. `SET_SLOT(0x31, [0, 0])` — 先选中槽位 0 才能写图（之前全黑的根因）
3. `WRITE_IMG(0x30, RLE flags)` — 平面按 RLE 压缩，flags：
   - 首块：`0x04 | 0x02 | (bw?0:1)`
   - 续块：`0x04 | (bw?0:1)`
4. `REFRESH(0x05)`

实测：黑底白字时钟图成功显示。固件 `rle=1` 必须用 RLE 压缩传输。

### 显示一下又变白 / 变黑？

| 现象 | 常见原因 | 处理 |
|------|----------|------|
| 先有图再**变白** | `SET_TIME`/日历模式强制 GUI 刷新（picture 模式会 fill 白）；或槽位轮播切到空槽 | 别跑 `time 1/2`；`send` 默认 `SET_SLIDE off`；同步时间后再推一次图 |
| 先有图再**变黑** | 发送末尾误发 `SLEEP(0x06)`（本固件会黑屏）；或 `INIT`/缺 `SET_SLOT` | 当前 `send` **不再发 SLEEP**；保持 `INIT(model)+SET_SLOT+WRITE+REFRESH` |
| `clock_enable=1` | 若进了时钟模式会每分钟全刷 | 不要 `./epaper.sh time 2`；只用 `img`/`quotas` |

环境变量：`EPAPER_SET_SLIDE_OFF=0` 可关掉「关轮播」命令（默认开）。

## 一键工具

```bash
./epaper.sh img clock.png     # 传任意图片
./epaper.sh clock             # 显示当前时间(黑底白字)
./epaper.sh quotas            # 拉取 codex/grok/kimi/opencode-go/Ollama Pro/Windsurf 额度并显示
./epaper.sh quotas --no-send  # 只生成 400×300 PNG + frame，不推 BLE
./epaper.sh quotas-loop 900   # 每 900 秒刷新额度面板（默认 15 分钟）
./epaper.sh quotas-loop 900 --start 09:00 --end 18:00   # 仅早九晚六刷新
./epaper.sh time              # 同步价签内部时钟
./epaper.sh scan              # 扫 BLE 设备
```

### 布局设计器（在线拖拽）

`design/index.html` 是一个**本地单文件**拖拽式排版工具（浏览器直接打开即可，无需安装）：

- 400×300 画布，块（标题/时间、六个额度行、道德经）可**拖动移动**、右下角**缩放**、侧栏改**字体字号/颜色开关/边框**、增删块。
- 用示例额度数据**实时渲染 BWR 预览**，所见即所得（像素严格黑/白/红三色）。
- 「导出 JSON」把布局存为 `data/layout.json`；「导入 JSON」可回读或粘贴改过的配置。
- 本地渲染时用 `--layout` 指定布局文件（缺省不传则用内置排版）：

```bash
# 设计页导出 data/layout.json 后：
./epaper.sh quotas --layout data/layout.json
python -m quotas --out-dir data/out --layout data/layout.json --send
```

布局 JSON 与 `tools/quotas/layout.py` 的 `render_layout()` 一一对应，块坐标即面板坐标。
内置默认排版见 `tools/quotas/layout.py` 的 `DEFAULT_LAYOUT`（与 `quotas` 无参渲染完全一致，单测 `test_render_layout_default_matches_builtin` 保证）。

### AI 额度面板（quotas）

从本机已登录凭据读取额度（**不**在源码里写密钥）：

| 服务 | 凭据位置 | 数据源 |
|------|----------|--------|
| codex | `~/.codex/auth.json` | `GET chatgpt.com/backend-api/codex/usage` |
| grok | `~/.grok/auth.json` | `GET cli-chat-proxy.grok.com/v1/billing?format=credits` |
| kimi | `KIMI_API_KEY` / `KIMI_CODING_API_KEY`，或 `~/.kimi-code/credentials/kimi-code.json`（kimi-code 登录 OAuth） | `GET api.kimi.com/coding/v1/usages`；OAuth 过期走 `auth.kimi.com/api/oauth/token` 刷新 |
| opencode-go | `~/.local/share/opencode/auth.json`；完整窗口需 `OPENCODE_GO_WORKSPACE_ID` + `OPENCODE_GO_AUTH_COOKIE` | 仪表盘 scrape 或 key 探测 |
| ollama-pro | `OLLAMA_API_KEY`（也支持 `OLLAMA_PRO_API_KEY`） | `GET ollama.com/api/usage`；session/weekly `usage` 比例转换为剩余百分比 |
| windsurf / Devin | Windsurf/Devin `state.vscdb`、`credentials.toml`，或 `DEVIN_BEARER_TOKEN` + `DEVIN_ORG` | Codeium SeatManagement 或 `app.devin.ai/.../billing/quota/usage`；日/周订阅额度剩余百分比 |

单个服务失败只显示 error/unavailable，不会中断其它行。

Ollama Pro 使用官方 API key 环境变量，启动额度刷新前设置一次即可：

```bash
export OLLAMA_API_KEY='<你的 Ollama API key>'
./epaper.sh quotas
```

Windows PowerShell：

```powershell
$env:OLLAMA_API_KEY = '<你的 Ollama API key>'
python -m quotas --out-dir data/out --send --bleprobe bleprobe.py
```

`/api/usage` 当前返回 session（约 5 小时）和 weekly（7 天）两个使用比例；若接口未返回精确重置时间，面板对应时间位置显示 `—`，不会猜测时间。

Windsurf/Devin 订阅额度默认从桌面端的 `state.vscdb` 或 CLI `credentials.toml` 读取登录凭据，
也可以设置 `WINDSURF_API_KEY`；Devin 网页额度接口则使用 `DEVIN_BEARER_TOKEN`（或
`DEVIN_AUTHORIZATION`）配合 `DEVIN_ORG`。接口返回日额度和周额度的剩余百分比，面板分别显示为
`day` 和 `wk`；本地缓存只在云端查询失败时使用。`DEVIN_API_KEY`（`apk_user_`）是 Devin
会话 REST key，官方没有把自助订阅 quota 暴露在该 API 上，因此不会误发到额度接口。

额度与时间均本地化显示：剩余额度统一百分比、重置时间显示为相对时间（`3天2时` / `35分`）；
顶部显示小字号农历 + 节气 + 天干地支，以及沪指/深成指/标普/纳指当日涨跌；行情取不到时使用上次本地缓存。
底部随机显示一句《道德经》（`tools/quotas/daodejing.json` 本地缓存）。

每次成功渲染会保存一份不含密钥的本地历史快照；有上次快照时，额度旁显示剩余百分比的 delta，
例如 `Δ-2.0` 表示比上次刷新少 2 个百分点，首次刷新没有 delta。
同时保存当天的刷新采样到 `epaper-quotas-history.json`，每个订阅右侧只显示一条总用量（已用百分比）折线图；
曲线旁的 plan 数值与 delta 使用更大的字体，顶部另显示沪/深/标普/纳指的当日涨跌；当天第一次刷新只有一个点，跨到新的一天会自动清空旧曲线。

差量刷新分两层：默认模式仍比较上一次额度快照，数据没变就跳过 BLE 推送；
`./epaper.sh quotas --force` 可强制推送（比如想更新头部时间戳）。

实验性局部刷新可显式开启：

```bash
# 先用上一帧建立基准；没有上一帧时自动整屏发送
python -m quotas --out-dir data/out --send --partial --bleprobe bleprobe.py

# 允许测试 BWR 红色平面局部刷新；需要确认具体面板波形可靠后再使用
python -m quotas --out-dir data/out --send --partial --partial-red --bleprobe bleprobe.py
```

局部刷新状态保存在 `epaper-quotas-last-frame.bin`。发送前比较黑/红两个平面，
将变化区域按 8 像素 X 边界切成窗口，通过 SSD1619 原始命令 `0x03/0x04` 写入，
再用局部更新序列激活。变化面积过大、矩形过多或红色平面变化（未指定
`--partial-red`）时自动走原有完整 `0x30/0x05` 刷新。当前 GR5513 固件是否允许
原始命令透传、以及 BWR 面板是否支持可靠红色局部波形，仍需在实机上验证。

Windows 本机任务 `AI Quota Epaper Refresh` 默认使用原有整屏刷新；局部刷新仅在
手动显式传入 `--partial`（以及需要时 `--partial-red`）时启用。局部传输仍需在
实机上验证，失败、变化面积过大或矩形过多时会回退完整刷新。

定时刷新（二选一）：

```bash
# A) 进程内循环（默认 900s）；可加活跃时段，窗口外自动跳过刷新
./epaper.sh quotas-loop 900
./epaper.sh quotas-loop 900 --start 09:00 --end 18:00   # 早九晚六
./epaper.sh quotas-loop 900 --start 22:00 --end 06:00   # 跨午夜

# B) launchd（StartInterval=900）
cp launchd/com.local.epaper-quotas.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.local.epaper-quotas.plist
```

单元测试：

```bash
python3 -m unittest discover -s tests -v
```

## 构建

```bash
swiftc -o build/bleprobe mac/BLEProbe.swift
```

## 使用

```bash
./epaper.sh img photo.png
./epaper.sh clock
./epaper.sh quotas
./epaper.sh time
```

## 目录结构

```
.
├── epaper.sh                 # 一键入口（img/clock/quotas/quotas-loop/time/scan/...）
├── mac/BLEProbe.swift        # BLE 扫描/GATT 分析/推帧 Swift 工具
├── build/bleprobe            # 编译出的二进制（gitignored）
├── tools/
│   ├── make_image.py         # PNG → 400x300 BWR frame.bin 平面转换
│   ├── clock_image.py        # 时钟图生成
│   └── quotas/               # AI 额度面板
│       ├── cli.py            # CLI + 差量刷新 + 定时窗口
│       ├── fetch.py          # 各 provider 额度拉取
│       ├── layout.py         # Apple 风格渲染 + 农历/节气 + 道德经栏
│       ├── credentials.py    # 本地凭据发现（密钥永不输出）
│       └── daodejing.json    # 道德经本地缓存
├── fonts/Roboto.ttf          # 英文字体（网络下载，随包附带）
├── launchd/                  # 定时刷新 LaunchAgent 模板
├── design/index.html         # 在线拖拽式布局设计器（单文件，浏览器直接打开）
├── tests/                    # 单元测试
└── data/                     # BLE 抓包/扫描（含真实 UUID/MAC，gitignored）
```

## 安全说明

- 额度凭据从**本机用户目录**的 `auth.json` / 环境变量读取，**源码里不保存任何密钥**。
- `credentials.py` 的 `redacted()` 保证日志/测试永不输出真实 token。
- `data/` 含价签真实 UUID 与抓包，已在 `.gitignore` 中排除，**请勿提交**。

## 许可

[MIT](LICENSE) © e-paper contributors. 第三方字体（Roboto，Apache 2.0）与本文许可证无关，见 `fonts/`。
