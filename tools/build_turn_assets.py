# -*- coding: utf-8 -*-
"""从 assets/pikachu_turn_v4.mp4 生成鼠标跟随帧资产（一次性构建脚本）。

流程：
  1. 用 imageio_ffmpeg 的 read_frames 解码出全部帧（RGB24 原始字节，无子进程）；
  2. 只取"转身段"的帧作为姿态坡道：f001（正面锚点）+ f013-f014 +
     f018-f028。f002-f012 只会动嘴、肉眼不可见，砍掉让鼠标小幅移动就
     进入明显转身段；跳过 f015-f017 眨眼，否则鼠标停在对应角度会永久闭眼；
  3. 抠掉黑色背景：对暗色像素做连通域标记，只把"与画面边界连通"的区域当
     背景。阈值取 24：背景是纯黑（p99=1），身体黑斑（耳尖等）因 h264 压缩
     带有亮度（p5≈42），转身时耳尖暗部即使与背景短暂连通也不会被判成背景。
     随后形态学清理：删小孤岛；按亮度填洞——洞内中位亮度 ≥20 的填回并恢复
     原始颜色（误抠的黑斑），纯黑的真镂空（尾巴与身体的缝隙）保持透明；
     腐蚀 1px 去掉贴边压缩暗边；
  4. 所有帧用同一 union 包围盒裁剪；在第 0 帧上用左右耳尖找出身体对称轴，
     把每帧贴到奇数宽画布、轴落在中心列——右向帧（整体镜像）与左向帧身体
     逐像素重合，换边时只有尾巴左右跳；
  5. 预乘 alpha BOX 缩放（高度优先，保证屏幕上的视觉大小）；相邻帧在
     "剪影 IoU 高且像素差小"的双重门槛下才做 50/50 混合补一帧中间帧，
     混合帧下标写入 manifest 供桌宠在静止时避开（混合帧只是运动中的
     动态模糊，静止画面永远落在真实关键帧上，不会发虚）；
  6. 最终定稿：alpha 高斯平滑后滞回双阈值二值化（边界整齐无噪点，细耳尖
     不闪），再清一次小孤岛，掩码外全部品红（tk transparentcolor 挖空），
     输出 left/NN.png 与镜像 right/NN.png。

用法：
    C:\\Software\\Miniconda\\envs\\moni\\python.exe tools\\build_turn_assets.py
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

ROOT = Path(__file__).resolve().parent.parent
VIDEO = ROOT / "assets" / "pikachu_turn_v4.mp4"
PROBE = ROOT / "runtime" / "_probe"
OUT = ROOT / "assets" / "turn"

# 视频里 1-based 帧号：正面 → 完全左侧影。
# f002-f012 与 f001 的逐帧差异全部低于可感知阈值（只会动嘴），砍掉，
# 让鼠标小幅移动就能进入明显的左转/右转段；f001 保留作正面锚点帧
# （换边必须经过的左右全同帧）。f015-f017 是眨眼，跳过防止永久闭眼。
RAMP = [1, 13, 14] + list(range(18, 29))
FIT_BOX_W = 232        # 缩放上限：身体对称轴居中后画布更宽，桌宠窗口会随之加宽
FIT_BOX_H = 174        # 高度优先：保证皮卡丘在屏幕上的视觉大小与单图时代一致
DARK_T = 24            # 低于该亮度视为"可能的背景"。背景纯黑(p99=1)、身体黑斑
                       # （耳尖等）因压缩亮度 p5≈42，取 24 两边都留足余量
MAGENTA = (255, 0, 255)

MIN_ISLAND = 120       # 源分辨率下小于该面积的可见孤岛视为压缩噪点删除
MAX_HOLE = 4000        # 大于该面积的内部镂空不填（耳尖内腔约千级像素）
HOLE_MIN_LUM = 20      # 洞内中位亮度低于它 = 纯黑真镂空（如尾巴与身体的缝隙），
                       # 高于它 = 被误抠的身体黑斑，填回并恢复原始颜色
ERODE_PX = 1           # 腐蚀掉贴边压缩暗边的宽度
FINAL_MIN_ISLAND = 24  # 定稿分辨率下的小孤岛阈值
EDGE_SIGMA = 0.8       # 定稿 alpha 高斯平滑强度
EDGE_STRONG = 110      # 定稿强阈值：平滑后高于它必属身体
EDGE_WEAK = 15         # 定稿弱阈值：与强区连通即保留（救回细耳尖）
INTERP_MIN_IOU = 0.90  # 相邻帧剪影重合度低于该值不补帧（大位移补帧会出重影）
INTERP_MAX_RGB_DIFF = 9.0  # 不透明区平均像素差高于该值不补帧（脸部错位会重影）


def decode_ramp_frames() -> list[Image.Image]:
    """解码视频并返回姿态坡道用到的帧（1-based 第 RAMP[n] 帧）。"""
    import imageio_ffmpeg
    gen = imageio_ffmpeg.read_frames(str(VIDEO))
    meta = next(gen)
    if isinstance(meta, (str, bytes, bytearray)):
        meta = json.loads(meta)
    w, h = meta["size"]
    need = w * h * 3  # rgb24
    wanted = set(RAMP)
    out: dict[int, Image.Image] = {}
    idx = 0
    for data in gen:  # 自然耗尽，避免中途 close 触发杀 ffmpeg 的句柄噪音
        idx += 1
        if len(data) < need:
            continue
        if idx in wanted:
            arr = np.frombuffer(data[:need], dtype=np.uint8).reshape(h, w, 3)
            out[idx] = Image.fromarray(arr.copy(), "RGB")
    missing = sorted(wanted - set(out))
    if missing:
        raise RuntimeError(f"视频帧不足：缺 {missing}")
    return [out[n] for n in RAMP]


def clean_subject_mask(subject: np.ndarray, lum: np.ndarray) -> np.ndarray:
    """删小孤岛、按亮度填洞。填洞恢复的是原始像素颜色（嘴线/耳尖等）。

    洞分两种：耳尖这类被误抠的身体黑斑因 h264 压缩带有亮度（中位远高于
    纯黑），要填回；尾巴与身体之间的真镂空透出的是纯黑背景（中位≈0），
    必须保持透明——只按亮度决定填不填。"""
    lab, n = ndimage.label(subject)
    if n > 1:
        sizes = np.bincount(lab.ravel())
        sizes[0] = 0
        subject = np.isin(lab, np.where(sizes >= MIN_ISLAND)[0])
    filled = ndimage.binary_fill_holes(subject)
    holes_lab, hn = ndimage.label(filled & ~subject)
    if hn:
        hsizes = np.bincount(holes_lab.ravel())
        hsizes[0] = 0
        for hid in np.where((hsizes > 0) & (hsizes <= MAX_HOLE))[0]:
            region = holes_lab == hid
            if float(np.median(lum[region])) >= HOLE_MIN_LUM:
                subject = subject | region
    return subject


def key_background(im: Image.Image) -> Image.Image:
    """返回带 alpha 的 RGBA：与边界连通的暗色区域 → 透明，并做形态学去噪。"""
    rgb = np.asarray(im.convert("RGB"), dtype=np.float32)
    lum = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    dark = lum < DARK_T
    lab, _ = ndimage.label(dark)
    border_labels = np.unique(np.concatenate([
        lab[0, :], lab[-1, :], lab[:, 0], lab[:, -1]]))
    bg = np.isin(lab, border_labels[border_labels != 0])
    subject = clean_subject_mask(~bg, lum)
    if ERODE_PX > 0:
        subject = ndimage.binary_erosion(subject, iterations=ERODE_PX)
    alpha = subject.astype(np.uint8) * 255
    return Image.fromarray(np.dstack([
        np.asarray(im.convert("RGB")), alpha]), "RGBA")


def union_bbox(frames: list[Image.Image]) -> tuple[int, int, int, int]:
    left = top = 10 ** 9
    right = bottom = -1
    for im in frames:
        bbox = im.getbbox()  # alpha>0 的包围盒
        if bbox is None:
            continue
        x0, y0, x1, y1 = bbox
        left, top = min(left, x0), min(top, y0)
        right, bottom = max(right, x1), max(bottom, y1)
    pad = 6
    return (max(0, left - pad), max(0, top - pad), right + pad, bottom + pad)


def estimate_body_axis(im: Image.Image) -> int:
    """在最正面的帧上用左右耳尖估计身体对称轴。

    正面姿势下两只耳朵关于身体轴近似对称，是全图最可靠的对称锚点：
    取每列最高不透明像素构成上轮廓，接近全局最高点的列分成左右两簇
    （左耳/右耳），两簇中位数的均值即对称轴。整帧镜像配准在这里不可
    用——尾巴只伸向一侧、3D 渲染有单侧光照，会把配准最小值带偏。
    """
    vis = np.asarray(im.split()[3]) == 255
    h, w = vis.shape
    top = np.where(vis.any(axis=0), np.argmax(vis, axis=0), h)
    min_top = int(top.min())
    tip_cols = np.where(top <= min_top + max(6, h // 80))[0]
    left = tip_cols[tip_cols < w // 2]
    right = tip_cols[tip_cols >= w // 2]
    if len(left) == 0 or len(right) == 0:
        return w // 2
    axis = int((np.median(left) + np.median(right)) / 2)
    if not 0.20 * w <= axis <= 0.65 * w:
        return w // 2  # 异常时退回画布中线
    return axis


def compose_body_centered(im: Image.Image, axis: int):
    """把帧贴到奇数宽画布上，使身体对称轴正好落在中心列。

    奇数宽保证中心列经 FLIP_LEFT_RIGHT 后映射回自身：镜像后的帧与原帧
    身体逐像素重合，只有尾巴换边。返回 (合成帧, 画布宽)。
    """
    w, h = im.size
    half = max(axis + 1, w - axis)
    cw = 2 * half + 1
    canvas = Image.new("RGBA", (cw, h), (0, 0, 0, 0))
    canvas.paste(im, ((cw - 1) // 2 - axis, 0))
    return canvas


def fit_resize(im: Image.Image) -> Image.Image:
    """预乘 alpha + BOX（面积平均）缩小。

    LANCZOS 有负瓣，1-2px 宽的耳尖经缩小后 alpha 随子像素相位忽大忽小
    （表现为耳尖闪烁）；BOX 的 alpha 就是真实几何覆盖率，细尖相位再偏
    也得到稳定正值。RGB 预乘后再缩放，避免透明区黑色渗进边缘像素。"""
    w, h = im.size
    scale = min(FIT_BOX_W / w, FIT_BOX_H / h, 1.0)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    if nw % 2 == 0:
        nw += 1  # 奇数宽才有中心列：镜像后身体逐像素重合（轴居中的前提）
    arr = np.asarray(im.convert("RGBA"), dtype=np.float32)
    a = arr[..., 3:] / 255.0
    pm = Image.fromarray(
        np.concatenate([arr[..., :3] * a, a * 255.0], axis=-1).astype(np.uint8),
        "RGBA").resize((nw, nh), Image.BOX)
    o = np.asarray(pm, dtype=np.float32)
    ao = o[..., 3:] / 255.0
    rgb = o[..., :3] / np.maximum(ao, 1e-6)
    return Image.fromarray(
        np.concatenate([rgb.clip(0, 255), ao * 255.0], axis=-1).astype(np.uint8),
        "RGBA")


def blend_mid(a_img: Image.Image, b_img: Image.Image) -> Image.Image:
    """预乘 alpha 域的 50/50 混合，避免直通 alpha 混合在透明区发黑。"""
    a = np.asarray(a_img, dtype=np.float32)
    b = np.asarray(b_img, dtype=np.float32)
    aa = a[..., 3:] / 255.0
    ab = b[..., 3:] / 255.0
    pm = (a[..., :3] * aa + b[..., :3] * ab) / 2.0
    am = (aa + ab) / 2.0
    rgb = pm / np.maximum(am, 1e-6)
    out = np.concatenate([rgb.clip(0, 255), am * 255.0], axis=-1)
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def mask_iou(a_img: Image.Image, b_img: Image.Image) -> float:
    am = np.asarray(a_img)[..., 3] >= 128
    bm = np.asarray(b_img)[..., 3] >= 128
    union = (am | bm).sum()
    return float((am & bm).sum()) / union if union else 1.0


def opaque_rgb_diff(a_img: Image.Image, b_img: Image.Image) -> float:
    """两帧共同不透明区域的平均像素差（0-255）。剪影重叠时脸部仍可能
    错位（转头时五官比轮廓动得快），该差值大的对混合必然重影。"""
    a = np.asarray(a_img, dtype=np.float32)
    b = np.asarray(b_img, dtype=np.float32)
    m = (a[..., 3] >= 128) & (b[..., 3] >= 128)
    if not m.any():
        return 0.0
    return float(np.abs(a[..., :3] - b[..., :3])[m].mean())


def interpolate(seq: list[Image.Image]):
    """相邻帧之间补中间帧，但只补内容高度相似的对，返回 (帧序列, 补帧下标)。

    双重门槛：剪影 IoU（轮廓错位）+ 不透明区像素差（脸部错位）。不达标
    的对宁可不补——混合帧只在运动中一闪而过充当动态模糊，静止画面永远
    落在真实关键帧上（pet 端按 manifest 的 blend_indices 吸附）。"""
    out = [seq[0]]
    blends = []
    for i, (prev, cur) in enumerate(zip(seq, seq[1:])):
        iou = mask_iou(prev, cur)
        diff = opaque_rgb_diff(prev, cur)
        if iou >= INTERP_MIN_IOU and diff <= INTERP_MAX_RGB_DIFF:
            out.append(blend_mid(prev, cur))
            blends.append(len(out) - 1)
        out.append(cur)
    print(f"补 {len(blends)} 帧 / 跳过 {len(seq) - 1 - len(blends)} 对"
          f"（IoU≥{INTERP_MIN_IOU} 且 像素差≤{INTERP_MAX_RGB_DIFF}）")
    return out, blends


def finalize_frame(im: Image.Image) -> Image.Image:
    """定稿：alpha 高斯平滑后滞回双阈值二值化，清小孤岛，掩码外品红挖空。

    单一阈值会闪掉细结构：缩放后耳尖只有 1-2px 宽，高斯平滑把尖端
    alpha 拉到阈值附近，随子像素相位不同忽存忽失（表现为耳尖闪烁）。
    滞回（弱像素只要连通到强区就保留）把细尖稳定救回，孤立噪点依然
    连不上强区、被挡在外面。"""
    arr = np.asarray(im.convert("RGBA")).copy()
    soft = ndimage.gaussian_filter(arr[..., 3].astype(np.float32), EDGE_SIGMA)
    strong = soft >= EDGE_STRONG
    weak = soft >= EDGE_WEAK
    lab, n = ndimage.label(weak)
    if n > 1:
        keep = np.unique(lab[strong & (lab > 0)])
        mask = np.isin(lab, keep[keep != 0]) if len(keep) else strong
    else:
        mask = strong
    lab, n = ndimage.label(mask)
    if n > 1:
        sizes = np.bincount(lab.ravel())
        sizes[0] = 0
        mask = np.isin(lab, np.where(sizes >= FINAL_MIN_ISLAND)[0])
    arr[..., 3] = np.where(mask, 255, 0)
    arr[...,:3][~mask] = MAGENTA
    return Image.fromarray(arr, "RGBA")


def main() -> int:
    raw = decode_ramp_frames()
    print(f"解码姿态坡道 {len(raw)} 帧")

    keyed = [(n, key_background(im)) for n, im in zip(RAMP, raw)]
    bbox = union_bbox([im for _, im in keyed])
    print(f"union bbox = {bbox}")
    crops = [im.crop(bbox) for _, im in keyed]

    # 身体对称轴只取自第 0 帧（最正面），全序列共用同一根轴、同一画布，
    # 保证帧间不跳动、镜像时身体重合
    axis = estimate_body_axis(crops[0])
    composed = [compose_body_centered(im, axis) for im in crops]
    cw = composed[0].size[0]
    print(f"身体对称轴 x={axis}，合成画布宽 {cw}（轴在中心列 {(cw - 1) // 2}）")

    resized = [fit_resize(im) for im in composed]
    interp, blends = interpolate(resized)
    finals = [finalize_frame(im) for im in interp]
    print(f"补帧后 {len(finals)} 帧/方向")

    (OUT / "left").mkdir(parents=True, exist_ok=True)
    (OUT / "right").mkdir(parents=True, exist_ok=True)
    for old in OUT.rglob("*.png"):
        old.unlink()

    sizes = set()
    for i, im in enumerate(finals):
        im.save(OUT / "left" / f"{i:02d}.png")
        # 右向 = 左向成品整体镜像：与左向逐像素互为镜像，身体重合
        im.transpose(Image.FLIP_LEFT_RIGHT).save(OUT / "right" / f"{i:02d}.png")
        sizes.add(im.size)
    print(f"输出 {len(finals)} 帧 × 2 方向，尺寸 {sizes}")

    manifest = {
        "source": VIDEO.name,
        "ramp_frames": RAMP,
        "key_frames": len(RAMP),
        "count": len(finals),
        "interpolated": True,
        "blend_indices": blends,
        "fit_box_w": FIT_BOX_W,
        "fit_box_h": FIT_BOX_H,
        "sizes": sorted(sizes),
        "body_axis_in_crop": axis,
        "canvas_width": cw,
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 联络表供人工检查抠像质量（蓝底衬托边缘）
    cols = 7
    rows = (len(finals) + cols - 1) // cols
    tw, th = FIT_BOX_W, FIT_BOX_H
    sheet = Image.new("RGB", (cols * tw, rows * th), "#3a7bd5")
    for i in range(len(finals)):
        fr = Image.open(OUT / "left" / f"{i:02d}.png")
        sheet.paste(fr, ((i % cols) * tw + (tw - fr.size[0]) // 2,
                         (i // cols) * th + (th - fr.size[1]) // 2), fr)
    PROBE.mkdir(parents=True, exist_ok=True)
    sheet.save(PROBE / "turn_sheet.jpg", quality=90)

    # 边缘特写：耳尖/脚部 4 倍放大供目检噪点
    fr = Image.open(OUT / "left" / "00.png")
    zoom = fr.resize((fr.width * 4, fr.height * 4), Image.NEAREST)
    zoom.save(PROBE / "edge_zoom.png")
    print(f"检查表：{PROBE / 'turn_sheet.jpg'}；边缘放大：{PROBE / 'edge_zoom.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
