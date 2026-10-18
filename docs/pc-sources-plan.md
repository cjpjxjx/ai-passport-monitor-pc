# PC 端画面源与 GUI 计划

状态：阶段一步骤 1、2（画面源抽象、桌面捕捉 `capture`，含 `--audio` 系统声音）已实现，发送节奏已简化（见 README），其余未开始。范围：`ai-passport-monitor-pc/`，设备端与 FAV1 协议不改。

## 目标

在现有 `play <文件>` 之外，增加三种由 PC 生成画面的模式，再加 GUI：

| 模式 | 画面来源 | 音频 |
| --- | --- | --- |
| 桌面捕捉 | ffmpeg `ddagrab` | 默认无音频轨；`--audio` 抓取系统声音（已实现） |
| 时钟 | Pillow 渲染 320x180 帧 | 无音频轨 |
| 硬件信息 | Pillow 渲染，数据来自 AIDA64 | 无音频轨 |

后续实现（不在本计划内）：静态图、GIF（需要设备落地存储，与 `monitor-plan` 的"不落地存储"决策冲突，先询问用户）。

## 约束

- 目标 30 fps、320x180，不降级；不达标按缺陷处理。
- 不改 `protocol.py` 与固件；画面仍走固定 3-3-2 调色板（`format=rgb8`）。
- 转 `format=rgb8` 的缩放器须带 `sws_dither=none`；输出选项 `-sws_dither none` 管不到滤镜图内部的转换，漏掉则静止区域逐帧不同、增量发送失效、每帧占满字节预算。
- 不新增测试代码。
- 模板格式自行设计为最简 JSON，不兼容原 C# 项目模板；不复制其源码与界面。
- GUI 框架：PySide6，跨平台（Win/Mac/Linux）。第一阶段只做 Windows。

## 已核实与未核实

| 项目 | 状态 |
| --- | --- |
| 本机 ffmpeg 7.1.1 带 `ddagrab`、`gdigrab` | 已核实 |
| `ddagrab` 在本机能稳定输出 30 fps | 已核实：ffmpeg 抓取与缩放链单独运行 30 fps；Wi-Fi 实机静态桌面 50 秒视频 30 包/秒、无慢写入。动态内容（视频、滚动）下未测 |
| 关闭抖动后增量发送生效 | 已核实：每帧中位字节约 20 kB 降到约 0.3 kB（离线测量，桌面静止为主） |
| `--audio` 的声音内容 | 离线已核实：3 秒测试音对应 77 个响亮块，峰值与预期一致，时间戳每块 40 ms；设备端听感未核实 |
| `--audio` 长时间音画漂移 | 未核实：声音来自声卡时钟，画面来自系统时钟 |
| 端到端延迟 | 发送节奏重写后未实测，设计值约 0.3 秒；旧实现约 2.8 秒（静音）、4.8 秒（`--audio`） |
| ffmpeg 同时读桌面与管道声音 | 已核实：画面每秒成批到达；声音改在本进程产生 |
| ffmpeg 从 stdin 读 rawvideo 并按帧率产出 | 未核实，步骤 3 前先验证 |
| 无限源没有音频时，`probe_audio` 的时长要求如何绕开 | 已核实：无限源跳过 `probe_audio`，轨道由源自身声明 |

## 阶段一：命令行

### 步骤 1：画面源抽象

- 先读 `server/live.py` 的 `source_command`、`source_graph`、`LiveChannel.start`，`server/tv_server.py` 的 `_live_session`、`main`。
- 新增 `server/sources.py`：定义"无限源"的 ffmpeg 输入参数构造（桌面捕捉、rawvideo stdin），与文件源并列；`LiveChannel` 按源类型选择输入参数，不在 `live.py` 内堆分支逻辑，只增加最小的接入点。
- 无限源没有文件时长：轨道由源自身声明，跳过 `probe_audio`；会话由用户中断结束，结束时发 END。
- 实时源不用 `-re` 读入，按 `-use_wallclock_as_timestamps` 或帧源自身节拍产出；确切方式在本步骤用最小实验确定。

### 步骤 2：桌面捕捉

- 新增子命令 `capture`：`--monitor <序号>`，输入 `ddagrab=framerate=30:output_idx=<序号>`，经 `hwdownload,format=bgra` 进入现有滤镜图（`frames.FIT` 之后量化）。
- 验证项：实测帧率是否稳定 30 fps，不达标则定位瓶颈（捕捉、缩放、条带压缩、链路），不改目标。
- `--audio`：`server/loopback.py` 用 `soundcard` 读取默认输出设备的 WASAPI 环回，在进程内降采样为 16 kHz 单声道，直接编码入队，不经 ffmpeg；默认无音频轨，不选设备。

### 步骤 3：渲染器

- 新增 `server/render/` 包，只依赖 Pillow，不依赖 GUI 库：
  - `canvas.py`：320x180 画布、图层按序绘制、文字（含描边、阴影、旋转）、图片；字体对象缓存。
  - `template.py`：JSON 模板读写与校验（图层列表：背景图、图片、文字、传感器数值，含坐标、字体、颜色、旋转）。
  - `clock.py`：时钟画面，按模板绘制当前时间。
  - `aida64.py`：读取 AIDA64 共享内存 `AIDA64_SensorValues`，失败回退注册表（仅 Windows，标准库 `mmap`、`winreg`）；非 Windows 平台返回不可用，不抛异常。
- 渲染帧经 stdin 以 rawvideo 送入 ffmpeg，由渲染线程按 30 fps 节拍写入；stdin 写入阻塞时丢帧而不是堆积。
- 新增子命令 `clock`、`hwinfo`：`--template <json>`。

### 步骤 4：文档与依赖

- `requirements.txt` 增加 `Pillow`（`soundcard` 已增加）。
- 更新 `README.md`。
- 命令行入口保持 `python -m server.tv_server <子命令>`。

### 阶段一验收

- 三个子命令在本机经 USB 各跑通一次完整会话，无协议违规日志。
- 报告桌面捕捉与渲染模式的实测帧率；设备画面与声音由用户自行判断。
- 测试项一律 `NOT RUN`；未在真机验证的内容列入 Unverified。

## 阶段二：GUI（PySide6）

- 新增 `gui/` 包，与 `server/` 分离；GUI 只负责配置与展示，发送端作为独立进程启动，通过命令行参数和日志管道通信，避免与编码线程争用 GIL。
- 功能顺序：
  1. 主窗口：选择模式、显示器、串口或 Wi-Fi 地址，开始与停止，状态与日志。
  2. 预览：显示渲染器当前输出帧。
  3. 模板编辑：图层列表、拖拽定位（`QGraphicsScene`）、属性面板、保存与加载 JSON。
- `requirements.txt` 增加 `PySide6`；打包（PyInstaller 或 Nuitka）留到功能稳定后再定。

## 跨平台

| 部分 | Windows | macOS | Linux |
| --- | --- | --- | --- |
| 桌面捕捉 | `ddagrab` | `avfoundation`，需屏幕录制权限 | `x11grab`，Wayland 走 PipeWire |
| 硬件信息 | AIDA64 | 无，需换数据源（如 `psutil`） | 同左 |
| USB 串口 | `pyserial` | 同左，端口命名与 DTR/RTS 行为待验证 | 同左 |
| 渲染器、协议、编码 | 通用 | 通用 | 通用 |

阶段一与阶段二只做 Windows；其他平台的捕捉源与数据源作为后续工作。

## 待用户决定

- 静态图、GIF 是否做，以及是否接受修改"不落地存储"决策。
- 固定 3-3-2 调色板出现色带时，是否允许改为按模板取样的调色板（需同步评估固件 PALETTE 包）。
- `play` 的滤镜链同样漏掉了滤镜图内部的抖动关闭，静止画面较多的视频可能受影响；是否同样处理（会改变已验证的 `play` 路径）。
- 原 C# 项目源码带"署名加相同方式共享"声明；本计划按"只参考思路、独立重写"处理。
