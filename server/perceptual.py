"""帧超出字节目标时，按感知代价逐步舍弃细节。

输入为 ffmpeg 固定 3-3-2 索引，能放进目标时原样发送。放不下时，每个像素可沿用
设备已显示的值或左邻像素的值（调色板颜色与参考像素足够接近时），使 deflate 看到
更多重复；接近程度的阈值按帧搜索，起点为上一帧所需档位。

三种模式共用逐帧档位搜索：

- `perceptual`：沿用已显示值或左邻。
- `perceptual_2d`：在此基础上再沿用上邻。
- `mosaic`：每个 N x N 块取左上角像素的颜色。

依赖 numpy，`AVAILABLE` 表示是否导入成功。
"""

from __future__ import annotations

try:
    import numpy as np
except ImportError:  # 无 numpy 时服务端退回颜色阶梯
    np = None

from . import frames

AVAILABLE = np is not None

WIDTH, HEIGHT = frames.WIDTH, frames.HEIGHT

# 参考颜色与替代颜色允许的距离，取加权平方 RGB（绿权重最大，蓝最小）。
# 档位从相邻调色板颜色的最小间距起步，更小则无可替换；0 档为参考本身。
LEVELS = (0, 3900, 7800, 11700, 15600, 23400, 31200, 46800, 70000, 110000)

# 各马赛克档位的块边长（像素）；0 档为参考本身。
MOSAIC_BLOCKS = (1, 2, 3, 4, 5, 6, 10)

MODES = ("perceptual", "perceptual_2d", "mosaic")

if AVAILABLE:
    # 与设备一致的调色板（红绿步长 36、蓝步长 85，见 frames.palette_bytes），
    # 权重折进坐标，使距离可直接按欧氏距离计算。
    _ROOT_WEIGHT = np.sqrt(np.array([3.0, 6.0, 1.0], np.float32))
    _PALETTE_W = np.array([((i >> 5) * 36, ((i >> 2) & 7) * 36, (i & 3) * 85)
                           for i in range(256)], np.float32) * _ROOT_WEIGHT


def _snap(reference: "np.ndarray", base: "np.ndarray", shown: "np.ndarray | None",
          held_distance: "np.ndarray | None", threshold: float,
          vertical: bool = False) -> "np.ndarray":
    """把 `base` 中的像素换成距离在 `threshold` 内的更省字节的索引。

    顺序为：设备已显示的值；逐列取左邻；`vertical` 时再逐行取上邻。
    邻居遍历必须顺序进行，像素可沿用刚沿用过邻居的像素。
    """
    out = base.copy()
    if shown is not None:
        keep = held_distance <= threshold
        out[keep] = shown[keep]
    columns = np.ascontiguousarray(out.T)
    source = np.ascontiguousarray(reference.transpose(1, 0, 2))
    for x in range(1, WIDTH):
        left = columns[x - 1]
        gap = _PALETTE_W[left] - source[x]
        near = (np.einsum("ij,ij->i", gap, gap) <= threshold) & (columns[x] != left)
        np.copyto(columns[x], left, where=near)
    out = np.ascontiguousarray(columns.T)
    if vertical:
        for y in range(1, HEIGHT):
            up = out[y - 1]
            gap = _PALETTE_W[up] - reference[y]
            near = (np.einsum("ij,ij->i", gap, gap) <= threshold) & (out[y] != up)
            np.copyto(out[y], up, where=near)
    return out


def _mosaic(base: "np.ndarray", block: int) -> "np.ndarray":
    """`base` 中每个 `block` x `block` 方块填成其左上角像素。"""
    small = base[::block, ::block]
    return np.repeat(np.repeat(small, block, axis=0), block, axis=1)[:HEIGHT, :WIDTH]


def encode_within(raw: bytes, shown: bytes | None, tick: int, target: int,
                  hint: int = 0, min_diff: float = frames.DELTA_MAX_DIFF,
                  mode: str = "perceptual") -> tuple[list[bytes], bytes, int]:
    """契约同 `frames.encode_within`，返回（待发条带，设备将显示的画面，档位）。

    先试 0 档（原帧）；放不下则从 `hint`（上一帧所需档位）起搜：放得下就再试低一档，
    放不下就在更高档位间二分。`mode` 取自 `MODES`，决定档位如何变成画面。
    """
    if len(raw) != frames.FRAME_PIXELS:
        raise ValueError(f"frame is {len(raw)} bytes, expected {frames.FRAME_PIXELS}")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    top = (len(MOSAIC_BLOCKS) if mode == "mosaic" else len(LEVELS)) - 1

    def render(level: int) -> bytes:
        if mode == "mosaic":
            return _mosaic(base, MOSAIC_BLOCKS[level]).tobytes()
        return _snap(reference, base, held, held_distance, LEVELS[level],
                     vertical=mode == "perceptual_2d").tobytes()

    def attempt(level: int):
        drawn = raw if level == 0 else render(level)
        return frames.choose_stripes(drawn, shown, tick, 1 << 30, min_diff), drawn

    def fits(result) -> bool:
        return frames._wire_size(result[0]) <= target

    result = attempt(0)
    if fits(result):
        return result[0], result[1], 0

    base = np.frombuffer(raw, np.uint8).reshape(HEIGHT, WIDTH)
    reference = _PALETTE_W[base]
    held = held_distance = None
    if shown is not None:
        held = np.frombuffer(shown, np.uint8).reshape(HEIGHT, WIDTH)
        gap = _PALETTE_W[held] - reference
        held_distance = np.einsum("hwc,hwc->hw", gap, gap)

    start = min(max(hint, 1), top)
    result = attempt(start)
    if fits(result):
        if start > 1:
            lower = attempt(start - 1)
            if fits(lower):
                return lower[0], lower[1], start - 1
        return result[0], result[1], start
    low, high, best = start + 1, top, None
    while low <= high:
        probe = (low + high) // 2
        result = attempt(probe)
        if fits(result):
            best, high = (result[0], result[1], probe), probe - 1
        else:
            low = probe + 1
    if best is not None:
        return best
    drawn = attempt(top)[1]
    return frames.choose_stripes(drawn, shown, tick, target, min_diff), drawn, top
