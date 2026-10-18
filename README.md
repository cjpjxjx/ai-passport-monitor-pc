# ai-passport-monitor-pc

把本地视频文件或 Windows 桌面画面转码后推送到 AI Passport 显示器应用（固件仓库 `ai-passport`，分支 `feature/monitor`）。

代码取自参考项目 `ai-passport-tv-server`（提交 `3e1c539`），转码、条带压缩与降质逻辑保留；发送节奏重写为按到达顺序发送，没有预缓冲与内容时间轴。与参考的差别：

- 源从直播频道表改为命令行给出的本地文件，ffmpeg 仍以 `-re` 读入。
- CONFIG 声明会话携带的轨道（`audio`、`video`）；没有音轨的文件与不带声音的 `capture` 只发画面，不发静音。
- 文件读完后发 END 并退出，不再等设备重连。
- 去掉频道表、`channel` 与 `channel_list` 字段、配对令牌、断流重连与叠字（`reconnect.py`、`overlay.py`）。
- CONFIG 只带固件校验的字段，各轨的字段随该轨声明；控制包上限与固件一致（1024）。

传输有 Wi-Fi（TCP）和 USB 两种。USB 由 `server/usb_bridge.py` 在本机 TCP 端口与设备串口之间转发，代码取自参考项目 `ai-passport-tv` 的 `tools/usb_bridge.py`，只去掉限速、限时与统计。

## 运行

需要 Python 3.10+、`numpy`（条带降质与环回降采样）、`pyserial`、`soundcard`（仅 `capture --audio`），以及 PATH 中的 `ffmpeg`、`ffprobe`。

```bash
pip install -r requirements.txt
python -m server.tv_server play <视频文件>
python -m server.tv_server play <视频文件> --bind <局域网 IPv4> --port 8096
python -m server.tv_server play <视频文件> --usb COM3
python -m server.tv_server capture --monitor 0 --usb COM3
python -m server.tv_server capture --audio
```

`capture` 用 ffmpeg `ddagrab` 抓取指定显示器（含鼠标指针），保持比例缩放到 320x180、不足处补黑边，固定 30 fps，仅支持 Windows。默认只有画面；`--audio` 经 WASAPI 环回抓取默认输出设备的声音（依赖 `soundcard`），在本进程内降采样为 16 kHz 单声道，不经 ffmpeg；本机扬声器照常出声，不能选择设备，切换默认设备需重启。没有结束时刻，Ctrl+C 时向设备发 END 后退出；设备中途断开则会话失败并退出。`--bind`、`--port`、`--usb`、`--ffmpeg` 同 `play`。

`play` 与 `capture` 的 `--hwaccel qsv` 启用 Intel 核显加速：文件源由 QSV 解码，两种源都先在 GPU 上缩到宽 640 再下载到内存，由软件滤镜完成最终的 320x180 缩放。启动时用真实输入试跑一帧，失败则回退软件路径并在日志中说明；默认 `none`。NVIDIA、AMD 未支持。

`play` 的文件源缩小后统一经 libplacebo（Vulkan）做色调映射，由它按每帧自带的色彩标记决定是否映射，不探测源格式；HDR 帧转成 BT.709 SDR，SDR 帧基本原样通过。启动时用合成画面试跑，Vulkan 不可用则不映射并在日志说明。算法由 `TV_TONEMAP` 选择（`auto` 默认）。`capture` 不做色调映射。

Wi-Fi 启动后会打印设备配网页面要填的电脑地址。设备没有信号时轮流检测 USB 与 Wi-Fi，各 5 秒，先收到 CONFIG 的一路开始播放；接着数据线时也能走 Wi-Fi，设备最多约 10 秒发现新启动的服务端。

`--usb <串口>` 通过设备的 USB 串口推流，服务器只监听 127.0.0.1，`--bind` 不生效。设备发出 HELLO 后会话才开始；打开串口前 DTR 与 RTS 置低，避免板子复位。

## 发送节奏

- 音频：读取线程把 40 ms 块放入 6 块的队列，满时文件源阻塞读取（ffmpeg 随之限速、不丢），无限源丢最旧块；发送端最多领先实时 `AUDIO_AHEAD_MS`（80 ms），块的时间戳是已发块数乘 40 ms。
- 画面：读取线程记录每帧到达时刻，发送端在到达后再等 `AV_LEAD_MS`（250 ms，仅带音频轨时）发出，使画面与声音同时到达观看者；文件源不丢帧，ffmpeg 起播时成批吐出的帧按帧间隔顺延发出；无限源（`capture`）同时到期的帧超过 3 个时跳到最新以保低延迟。画面时间戳在带音频轨时取发送时刻的音频位置，否则取帧序号乘帧间隔，严格递增。
- 固件起播门槛 `AUDIO_START_CHUNKS` 为 2 块（80 ms），设备音频队列之后保持在该深度附近。
- 没有音频轨的源（无音轨文件、不带 `--audio` 的 `capture`）不延后画面。

`TV_` 开头的环境变量用于调参，名称与默认值见 `grep -n 'environ.get("TV_' server/*.py`。

## 验证情况

用遵守固件流规则的假设备（包序号连续、仅声明的轨道出现包、音频时间戳间隔 40 ms、画面时间戳严格递增）连接 `play`（4 秒带音轨与无音轨片段）与 `capture`（20 秒），均无违规；无音轨片段与 `capture` 的 CONFIG 不含音频字段、没有音频包，画面约 30 fps。`capture --audio` 未复测。真机（Wi-Fi）播放无音轨视频、有音轨视频（各 40 秒）与 `capture`（25 秒），画面约 30 fps，PC 端丢帧 0；首次连接有一次会话写入超时失败，设备重连后正常，原因未查明。USB 传输、画面与声音观感、音画同步、`capture --audio`、长时间运行未验证。`--audio` 的环回声音用 1 kHz 测试音核对，降采样后主频为 1000 Hz。ffmpeg 同时读桌面画面与管道声音时画面成批到达（每秒一批约 30 帧），声音改由本进程产生后消除。

`--hwaccel qsv`（Intel Iris Xe，ffmpeg 7.1.1，不接设备）：4K HDR 60 fps webm 的 ffmpeg 进程 CPU 约 280% 单核降到约 18%，桌面捕捉（2048x1152）约 156% 降到约 27%，均稳定 30 fps、不丢帧；同一帧两条路径导出对比，PSNR 34 dB、目视无差别。接真机（USB）：4K 视频推送约 47 秒（`video_sent` 1937）、桌面捕捉约 50 秒（1451），均约 30 fps，PC 端与设备端丢帧 0；画面观感、增量发送在动态桌面内容下的字节量、非 16:9 源未验证。

色调映射（libplacebo，测试 HDR 文件为 PQ、BT.2020，峰值 1000 nit）：同一帧平均亮度从 67.6 升到约 78，最亮值从 160 升到约 220；SDR 文件平均亮度 109.5 变为约 109，基本不变。缩小到 640 宽后再映射，软件路径与 QSV 路径均 30 fps，QSV 路径 ffmpeg CPU 约 15%，软件路径约 283%；4K 整帧直接交给 libplacebo 只有 19 至 25 fps。真机（USB）推送 HDR 视频 45 秒，丢帧 0。HLG 源、桌面开启 Windows HDR 时的 `capture`、SDR 文件上真机观感未验证。

未验证（均需真机）：端到端延迟（设计值约 0.3 秒，推算）；固件 `AUDIO_START_CHUNKS=2` 的欠载表现；音画是否对齐，`AV_LEAD_MS` 未实测，见 [docs/av-sync-handoff.md](docs/av-sync-handoff.md)；`capture` 在真机上的帧率；长时间运行的音画漂移。

已知问题：高码率内容（每帧约 15 至 17 KB）在 Wi-Fi 上播放时间歇出现写入卡顿与音频欠载，出现后不恢复，根因未查明，见固件仓库 `docs/development/monitor-progress.zh_CN.md`。

`play` 的滤镜链没有关闭滤镜图内部转 `rgb8` 的抖动，静止画面较多的视频可能每帧都近乎整帧重发；`capture` 已关闭。
