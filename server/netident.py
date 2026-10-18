"""自动识别设备应填写的服务端地址。

给出两种形式：

* 名称（`<主机名>.local`）：填到设备上。路由器重新分配 IP 后名称仍然有效。
* 数字地址（`192.168.1.20`）：套接字绑定需要字面地址，不能监听名称。

名称查询方式因系统而异，逐一尝试；失败就报告"名称未知"，不猜测，错误的名称比没有名称更糟。
"""

from __future__ import annotations

import platform
import socket
import subprocess

# 标准库无法枚举网卡。把 UDP 套接字 connect 到远端地址不会发包，但内核会选出出口网卡，
# 取套接字本端地址即可。优先用保留的文档地址，即使误发也不会到达任何主机。
_PROBE_ADDRESSES = (
    ("192.0.2.1", 9),        # RFC 5737 TEST-NET-1
    ("198.51.100.1", 9),     # RFC 5737 TEST-NET-2
    ("8.8.8.8", 53),         # 最后兜底：总有路由的地址
)


def lan_address() -> str | None:
    """同一网络中的设备访问本机所用的 IPv4 地址；没有任何出口路由时返回 None。

    不用回环地址代替：127.0.0.1 只有本机能访问，填到设备上会表现为服务端故障。
    """
    for address, port in _PROBE_ADDRESSES:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.5)
            sock.connect((address, port))
            found = sock.getsockname()[0]
        except OSError:
            continue
        finally:
            sock.close()
        # 回环地址说明探测没有得到可用结果，继续尝试下一个。
        if found and not found.startswith("127."):
            return found
    return None


def _run(command: list[str]) -> str | None:
    """返回命令输出的第一行，失败返回 None，不抛异常。

    显式指定 UTF-8：机器名可能含非 ASCII 字符，按平台默认编码读取可能在读取线程中抛异常。
    """
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    line = done.stdout.strip().splitlines()
    return line[0].strip() if line else None


def local_name() -> str | None:
    """本机的 `.local` 名称，无法确定时返回 None。

    * macOS：`scutil` 给出 Bonjour 名称。
    * Linux/BSD：`avahi-resolve` 向运行中的 mDNS 服务询问，没有 avahi 就没有名称。
    * Windows：取环境变量中的计算机名，系统以 `<名称>.local` 发布 mDNS。

    只返回带 `.local` 后缀的名称；裸主机名在家用网络中无法解析。
    """
    system = platform.system()

    if system == "Darwin":
        name = _run(["scutil", "--get", "LocalHostName"])
        return f"{name}.local" if name else None

    if system == "Windows":
        import os
        name = os.environ.get("COMPUTERNAME")
        return f"{name}.local" if name else None

    # Linux、BSD 及其他运行 mDNS 服务的系统。
    name = _run(["avahi-resolve", "--address", "-n", socket.gethostname()])
    if name:
        return name.rstrip(".") + ".local" if not name.endswith(".local") else name
    return None


def describe(address: str | None = None, port: int = 8096) -> list[str]:
    """启动时打印的地址提示行。

    `address` 为作为备用显示的局域网地址；调用方没找到时为 None（例如监听所有网卡且没有私有地址）。
    """
    name = local_name()

    # 不打印监听地址：设备填的是名称，数字地址只在名称失效时作为备用出现。
    lines: list[str] = []
    if not address:
        if name:
            lines.append("设备上要填的地址：")
            lines.append(f"    {name}:{port}")
            lines.append(f"（连不上就换成这台电脑的局域网地址:{port}）")
        else:
            lines.append(f"没有识别出这台电脑的局域网地址。设备上填它的局域网地址:{port}。")
        lines.append("")
        return lines

    # 绑定到回环地址时局域网设备无法连接，下面的填写建议不适用，改为说明原因。
    if address.startswith("127."):
        lines.append("这是一个本机地址（127 开头），只有这台电脑自己能访问，")
        lines.append("局域网里的设备连接不上。")
        return lines

    # 只给一个待填的值，下面至多一行备用说明。
    if name:
        lines.append("设备上要填的地址：")
        lines.append(f"    {name}:{port}")
        lines.append(f"（连不上就换成 {address}:{port}）")
    else:
        lines.append("设备上要填的地址：")
        lines.append(f"    {address}:{port}")
        lines.append("（读不到这台电脑的名字；路由器换 IP 后要重新填一次）")
    # 末尾空行，把后续输出与填写说明隔开。
    lines.append("")
    return lines
