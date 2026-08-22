# ⚡ 皮卡丘

本地通知总线 + 桌宠气泡 + 健康提醒。三块互相独立、可单独使用，只通过一条
消息协议（`pika.protocol.Notification`）通信。

- **pika-bus**：监听 `127.0.0.1:8765` 的消息总线。任何软件 POST 一条 JSON 进来，
  桌宠就弹气泡。不轮询，推送靠 SSE 长连接。
- **pika-pet**：桌面桌宠（tkinter，仅 Windows）。只负责"显示"：透明置顶小窗口、
  气泡、拖动、右键菜单、隐藏到角落。不感知消息来源。
- **pika-reminder**：健康提醒调度器。纯逻辑、平台无关，Windows 空闲检测作为
  注入的 ActivitySource 实现。定时随机提醒（默认每 1~2 小时）+ 久坐提醒
  （连续工作超阈值）。
- **pika-adapter-zcode**：ZCode 自动化 → 总线的适配器，一行命令发通知。

目录完全不依赖 codex / 旧皮卡丘，只有 `assets/` 三张贴图来自旧桌宠素材。

## 快速开始

```bash
# 1. 启动桌宠（内嵌总线，占用 8765 端口）
python pikachu.py pet

# 2. 发一条气泡看看（在另一个终端）
python pikachu.py send "你好" "我是皮卡丘，你写代码超过一小时了"

# 3. 启动健康提醒（另开终端）
python pikachu.py reminder

# 4. ZCode 自动化激活时通知（hook 里调用）
python pikachu.py zcode "每日简报" --stage done --detail "生成 3 个文件"
```

依赖：Python 3.9+，tkinter（桌宠）、Pillow（贴图透明处理，可选）。全部标准库
实现总线与提醒，无需 pip 安装任何东西。

## 消息协议

任何软件（不限 Python）向总线 POST（必须 `Content-Type: application/json`）：

```bash
curl -X POST http://127.0.0.1:8765/notify \
  -H "Content-Type: application/json" \
  -d '{"title":"该休息了","body":"起来走走","level":"warn","source":"reminder","ttl":10}'
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `title` | 是 | 气泡标题 |
| `body` | 否 | 补充说明，可换行 |
| `level` | 否 | `info` / `success` / `warn` / `error` |
| `source` | 否 | 来源标识（用于去重和统计） |
| `ttl` | 否 | 自动消失秒数；`0` = 常驻直到点击 |
| `ts` | 否 | epoch 秒，不填由总线补齐 |

可靠性设计：
- 桌宠挂 SSE 长连接实时收消息，**不轮询**；
- 断线重连自动带 `?after=<mid>` 增量补拉，只补错过的消息，不重放旧消息；
- 总线重启（跨进程 pid 变化或同进程重建的 generation 代次变化）后客户端自动
  重置增量游标，全量补拉；历史消息（超过 60 秒的旧消息）只计数不弹泡；
- SSE 连接建立时服务端立即发送带内身份行 `: gen=<代次> pid=<进程号>`，客户端
  发现身份与已知不同就断开、清游标、无游标重连全量补拉——即使总线在客户端
  健康探测的间隙完成重启（旧游标会把该补送的消息过滤掉），也不会丢消息；
- 总线进程被意外 kill 时，客户端在心跳超时（默认 20 秒）内检测到并自动重连；
- 慢订阅者不阻塞发布方：队列满的连接会被断开，由客户端重连补拉；
- 本机端口被其他软件占用时，桌宠内嵌总线自动回退到随机端口，并把实际端口
  写入 `runtime/port`，同时打印提示。

## 模块与命令

```text
python -m pika.bus          # 独立总线（默认 8765）
python -m pika.cli send ... # 命令行发通知
python -m pika.cli history  # 查看最近消息
python -m pika.cli health   # 查看总线状态
python -m pika.pet          # 桌宠（--subscribe-only 订阅外部总线）
python -m pika.reminder_runner   # 健康提醒
python -m pika.adapters.zcode <名称> --stage done --detail "说明"
python pikachu.py doctor    # 环境自检
```

## 健康提醒配置

编辑 `pika/configs/reminder.json`（`reminder_runner` 会自动发现；`--config` 可指定）：

```json
{
  "interval_enabled": true,
  "interval_min": 60,
  "interval_max": 120,
  "categories": ["eye", "neck", "water", "stand", "screen", "walk", "posture"],
  "long_session_enabled": true,
  "long_session_min": 90,
  "rest_min": 5,
  "long_categories": ["stand", "neck"],
  "title": "该休息一下了"
}
```

- `interval` 通道：每 `interval_min`~`interval_max` 分钟内的随机时刻提醒一次
  （人空闲超过 30 分钟则顺延，不打扰）；
- `long_session` 通道：连续工作（键盘/鼠标空闲未达 `rest_min` 分钟）累计超过
  `long_session_min` 分钟提醒一次，发完重置计时，直到出现一次足够长的休息
  才重新武装。

内置七类文案（`categories` 可自由增删）：`eye` 看远处护眼、`neck` 活动脖子、
`water` 喝水、`stand` 站起来走走、`screen` 离屏幕远一点、`walk` 出门散步、
`posture` 坐姿检查；久坐长休息从 `long_categories` 里取。每类多条文案按权重
随机轮换，不会连续重复同一句。

文案在 `pika/reminder_phrases.py`，分类 + 权重，加文案不用改代码。

## ZCode 集成

### 会话完成气泡（已接通）

`~/.zcode/cli/config.json` 注册了 `Stop` 钩子（`hooks.enabled: true`）：
每次回复完成，ZCode 以 process 方式调用 `tools/zcode_hook.py`，弹气泡
「会话完成 · <会话标题>」——标题按 stdin 字段 → 客户端会话库
`~/.zcode/cli/db/db.sqlite` 的 `session.title`（只读查询，与界面显示一致）
→ 转录首条用户消息 → 目录名兜底；正文是最后一条回复的开头 120 字符。
钩子永远 exit 0、总线不在时静默；原始 stdin 落在
`runtime/hook_stdin.log` 供排查。测试见 `tests/test_zcode_hook.py`。

### 自动化的开始/结束汇报（提示词约定）

钩子事件里没有自动化专用信号，自动化的"开始/结束"靠在自动化提示词里
写两行 CLI 调用（结束时 Stop 钩子也会再报一次会话完成）：

```bash
python /d/_Project/pikachu/pikachu.py zcode "每日简报" --stage start
python /d/_Project/pikachu/pikachu.py zcode "每日简报" --stage done --detail "生成 3 个文件"
python /d/_Project/pikachu/pikachu.py zcode "watch-inbox" --stage error --detail "权限不足"
```

`--stage` 决定图标与配色：`start`(▶ 灰) / `done`(✅ 绿) / `error`(❌ 红) / `run`(▶)。
创建自动化时把第一行放进任务开头、第二/三行按结果放在结尾即可。

## 桌宠交互

- 悬浮：显示状态气泡（已收通知数 / 来源统计 / 最近 3 条）；
- 通知气泡：有 `ttl` 自动消失；悬浮其上不消失；点击立即关闭；
- 气泡外观：Canvas 手绘的**像素风切角卡片**，致敬 pilog（pixel / minimal 主题）——
  纸白底 `#FBF8EE` + 2px 墨色硬边框 + 无模糊的偏移硬阴影，左侧强调色条 + 方形徽章
  （info 皮卡丘黄⚡手绘闪电 / success 绿✓ / warn 橙! / error 红✕），等宽字体
  （Cascadia Code）分级排版（标题加粗、正文、来源·时间小字），中文自动回退雅黑，
  文本按像素宽折行、超宽自动换行，位置钳回屏幕内，底边小尾巴正对皮卡丘脑袋；
- **缩放**：鼠标滚轮或右键菜单可放大/缩小**桌宠**（像素素材按 NEAREST 重渲染，
  放大不糊、保持算法像素味）；右键菜单还可单独放大/缩小**气泡**（字号/内距/尾巴
  同步缩放）；
- 右键菜单：自绘像素风切角卡片（纸白面 + 硬边 + 硬阴影 + 悬停反色），含
  显示状态 / 立即提醒一次 / 静音开关 / 放大缩小桌宠 / 放大缩小气泡 /
  隐藏到角落 / 关于 / 退出；点击菜单外任意处、按 Esc、或失焦即自动关闭；
- **跟随移动**：拖动皮卡丘时，气泡中心始终贴着皮卡丘脑袋（尾巴正对脑袋），
  随皮卡丘同步移动、缩放后同样重定位；开着的右键菜单也跟随皮卡丘挪动；
- 静音：消息照常记录与去重，但不弹气泡；
- 隐藏后：屏幕右下角出现 ⚡ 标签，点一下唤回；
- **鼠标跟随**：皮卡丘会连续转身看向鼠标（~30fps）。素材只有朝左的转身
  （`assets/pikachu_turn_v4.mp4`），朝右为镜像帧；换边永远经过正面帧，
  不会出现镜像瞬移。资产缺失时自动回退静态贴图。

## 转身帧资产（重新生成）

`assets/turn/{left,right}/NN.png` 由 `tools/build_turn_assets.py` 从视频一次性
生成：解码 → 边界连通域抠黑底（阈值 24：背景纯黑、身体黑斑因压缩带亮度，
转身时耳尖暗部不会误连背景）→ 形态学去噪（删孤岛/按亮度填洞——误抠的黑斑
填回原色，纯黑的尾巴缝隙保持透明/腐蚀压缩暗边）→ 全序列统一裁剪 → 左右耳尖
定身体对称轴并居中合成（换边时身体逐像素重合，只有尾巴左右跳；缩放强制奇数宽
保住该性质）→ 预乘 alpha BOX 缩放（高度优先；面积平均的 alpha 让细耳尖不随
子像素相位闪烁）→ 滞回双阈值二值化定稿 → 相邻帧自适应补中间帧（剪影重合度
≥0.90 才补，大位移区段不补以免重影）。坡道砍掉了只会动嘴的 f002-f012（鼠标
小幅移动就能进入明显转身段），跳过眨眼帧 f015-f017（避免鼠标停在对应角度时
永久闭眼）。混合补帧只用于运动中（动态模糊），桌宠静止时按 manifest 的
`blend_indices` 吸附到最近的真实关键帧，画面永远清晰。改参数后重建：

```bash
python tools/build_turn_assets.py   # 需 PIL + scipy（如 numpy/ndimage）
```


## 架构

```text
adapter-zcode ─┐
其他软件 ──────┼─► pika-bus（HTTP + SSE）─► pika-pet（桌宠）
pika-reminder ─┘
```

依赖方向只有一种：发送方 → 总线 → 显示端。发送方不需要知道桌宠是否存在；
桌宠不需要知道消息从哪来。Windows 专用代码只存在于 `pika/pet.py` 和
`pika/win/idle.py`，其余模块可跨平台运行。

## 测试

```bash
python -m unittest discover -s tests -v        # 单元测试
python tests/e2e/run_all.py                     # 端到端测试（起真进程）
```

单元测试 143 个，端到端 17 项。端到端覆盖：总线 CLI 往返、桌宠进程内嵌总线收发、
提醒器→总线→桌宠全链路、SSE 心跳保活、跨进程总线重启恢复、GUI 悬浮绑定、
转身朝向决策（滞回/平滑/经过正面换边）等。
测试全部使用随机端口，不依赖 8765 未被占用。
