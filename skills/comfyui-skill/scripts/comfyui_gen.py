#!/usr/bin/env python3
"""ComfyUI HTTP 生图客户端（两种底层共用的一份，装在 ~/.claude/skills/comfyui-skill/scripts/）。

用法：python3 comfyui_gen.py <bot_id> "<英文 prompt>" [--size 预设] [--neg 额外负向] [--out 路径]
                             [--global <_global.yml 路径>] [--bot-config <这只 bot 的 yml 路径>]

不 import 任何仓库里的模块，配置按下面的顺序找：
- 全局配置（moments.image_generation 下的 comfyui 设置与正负前缀）：
  --global → 环境变量 CLAUDEBOT_GLOBAL_CFG → $CLAUDEBOTLIFE_REPO/configs/_global.yml
- 这只 bot 的锚点（face_traits、anchor_image、anchor_denoise）：
  --bot-config → $CLAUDEBOTLIFE_REPO/configs/<bot_id>.yml；都没有就不用锚点。

成功时 stdout 最后一行是 `MEDIA: <图片路径>`；失败打印一行 `FAIL: <原因>`，退出码 1。

流程：
1. 读取 workflow JSON 模板
2. 替换 prompt 占位符 + 拼前缀 + 随机 seed
3. POST /prompt 排队
4. 轮询 /history/<prompt_id>（ComfyUI 报错立即返回）
5. 从 ComfyUI/output/ 找输出图，复制到 bot 目录；放进 ComfyUI/input/ 的起点图用完即删
"""
import sys
import os
import re
import json
import time
import shutil
import secrets
import argparse
import urllib.request
import urllib.error


class ComfyError(Exception):
    """生图失败，消息就是 FAIL 行里的原因。"""


# bot_id 会拼进配置与存图路径：只收字母、数字、下划线、点、连字符，且不能含 ".."
_BOT_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


def valid_bot_id(bot_id: str) -> bool:
    return bool(_BOT_ID.match(bot_id or "")) and ".." not in bot_id


def resolve_global_cfg(arg: str = None) -> str | None:
    """全局配置路径：--global → CLAUDEBOT_GLOBAL_CFG → $CLAUDEBOTLIFE_REPO/configs/_global.yml。"""
    repo = os.environ.get("CLAUDEBOTLIFE_REPO")
    for p in (arg, os.environ.get("CLAUDEBOT_GLOBAL_CFG"),
              os.path.join(repo, "configs", "_global.yml") if repo else None):
        if p:
            return os.path.expanduser(p)
    return None


def resolve_bot_cfg(arg: str, bot_id: str) -> str | None:
    """这只 bot 的 yml：--bot-config → $CLAUDEBOTLIFE_REPO/configs/<bot_id>.yml（存在才用）。"""
    if arg:
        return os.path.expanduser(arg)
    repo = os.environ.get("CLAUDEBOTLIFE_REPO")
    if repo:
        p = os.path.join(repo, "configs", f"{bot_id}.yml")
        if os.path.exists(p):
            return p
    return None


def _load_yaml(path: str) -> dict:
    import yaml
    with open(path, encoding="utf-8") as f:
        d = yaml.safe_load(f)
    return d if isinstance(d, dict) else {}


def _http_post(url: str, data: dict, timeout: int = 30) -> dict:
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_get(url: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def submit(comfyui_url: str, workflow: dict, client_id: str = None) -> str:
    if client_id is None:
        client_id = secrets.token_hex(8)
    out = _http_post(f"{comfyui_url.rstrip('/')}/prompt",
                     {"prompt": workflow, "client_id": client_id})
    pid = out.get("prompt_id")
    if not pid:
        raise ComfyError(f"ComfyUI 没收下这次请求: {out}")
    return pid


def wait_done(comfyui_url: str, prompt_id: str, timeout: int = 600) -> dict:
    """轮询 history。返回 history[prompt_id] 节点输出；ComfyUI 执行报错立即返回，不白等到超时。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            h = _http_get(f"{comfyui_url.rstrip('/')}/history/{prompt_id}")
            if h and prompt_id in h:
                entry = h[prompt_id]
                status = entry.get("status", {}) or {}
                if status.get("status_str") == "error":
                    raise ComfyError(f"ComfyUI 执行报错: {status.get('messages')}")
                if status.get("completed"):
                    return entry
                last = entry
        except (urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(2)
    raise ComfyError(f"ComfyUI 生图超时（{timeout}s）。最后状态: {last}")


def find_output_image(history_entry: dict, output_dir: str) -> str | None:
    """从 history 里找 SaveImage 节点输出的文件名。"""
    outputs = history_entry.get("outputs", {})
    for node_id, node_out in outputs.items():
        for img in node_out.get("images", []):
            sub = img.get("subfolder", "")
            fname = img.get("filename", "")
            if not fname:
                continue
            full = os.path.join(output_dir, sub, fname) if sub else os.path.join(output_dir, fname)
            if os.path.exists(full):
                return full
    return None


# 尺寸预设（ComfyUI 端 px / 总像素 ~1M 内符合 z-image-turbo 训练分辨率）
# img2img 模式下：anchor 会被等比缩放到该尺寸框内，输出尺寸=缩后 anchor 尺寸
# Mac MPS 后端慢，建议 img2img 用 quick 档
SIZE_PRESETS = {
    "tiny": (512, 768),            # 极速 2:3 — ~0.4MP，预览/快速发圈 (~25s on Mac)
    "quick": (640, 960),           # 标准 2:3 — ~0.6MP，朋友圈日常 (~45s)
    "portrait": (768, 1152),       # 高质 2:3 — ~0.88MP，重点作品 (~70s)
    "landscape": (1152, 768),      # 横图 3:2 — 风景/双人
    "square": (1024, 1024),        # 方图 1:1 — 头像/食物/物品
    "small": (512, 512),           # 小方图 — 快速预览/缩略
    "tall": (640, 1216),           # 长竖 ~1:1.9 — 全身/超长腿
    "wide": (1216, 640),           # 长横 ~1.9:1 — 屏幕宽景
    "wide16": (1280, 720),         # 精确 16:9 — 标准宽屏
}
DEFAULT_SIZE = "quick"  # 默认走标准档（img2img 模式下 ~45s）


def patch_workflow(workflow: dict, prompt: str, negative: str,
                   prompt_ph: str, neg_ph: str,
                   size: str = DEFAULT_SIZE,
                   init_image_name: str = None,
                   denoise: float = None) -> dict:
    """替换 workflow 占位符 + 随机 seed + 改尺寸 + 可选注入 init image / denoise。"""
    wf = json.loads(json.dumps(workflow))
    w, h = SIZE_PRESETS.get(size, SIZE_PRESETS[DEFAULT_SIZE])
    for node_id, node in wf.items():
        inputs = node.get("inputs", {})
        if "text" in inputs and isinstance(inputs["text"], str):
            t = inputs["text"]
            for ph, val in [(prompt_ph, prompt), (neg_ph, negative)]:
                if ph in t:
                    t = t.replace(f'"{ph}"', val).replace(ph, val)
            inputs["text"] = t
        if "seed" in inputs and isinstance(inputs["seed"], int):
            inputs["seed"] = secrets.SystemRandom().randint(1, 2**63 - 1)
        if node.get("class_type") in ("EmptySD3LatentImage", "EmptyLatentImage"):
            inputs["width"] = w
            inputs["height"] = h
        # img2img: 注入 LoadImage 文件名
        if node.get("class_type") == "LoadImage" and init_image_name:
            inputs["image"] = init_image_name
        # img2img: 注入 KSampler denoise
        if node.get("class_type") == "KSampler" and denoise is not None:
            inputs["denoise"] = denoise
    return wf


def _stage_init_image(abs_path: str, input_dir: str, target_size: tuple = None) -> str | None:
    """把 init-image 复制到 ComfyUI/input/ 用 unique 名字，返回文件名供 LoadImage 用。

    target_size=(target_w, target_h)：cover-crop 到精确目标尺寸。
    1) 如果 anchor 比例 != 目标比例 → 中心裁切到目标比例，再 resize
    2) 如果 anchor 比例 == 目标比例 → 直接 resize
    img2img 输出尺寸=init image 尺寸，必须严格匹配 SIZE_PRESETS，
    否则 --size wide 之类的指令传了也没用（anchor 是竖图等比缩放永远达不到 16:9）。
    宽高都对齐到 16 的倍数（z-image 官方硬约束，避免 latent 边缘错位）。
    """
    if not abs_path or not os.path.exists(abs_path):
        return None
    os.makedirs(input_dir, exist_ok=True)

    if target_size:
        from PIL import Image
        img = Image.open(abs_path).convert("RGB")
        w0, h0 = img.size
        target_w, target_h = target_size
        # 对齐到 16 的倍数
        target_w = max(16, (target_w // 16) * 16)
        target_h = max(16, (target_h // 16) * 16)

        src_ratio = w0 / h0
        dst_ratio = target_w / target_h

        if abs(src_ratio - dst_ratio) > 0.01:
            # 比例不同 → cover-crop（中心裁切到目标比例）
            if src_ratio > dst_ratio:
                # 源更宽：裁掉左右
                new_w0 = int(h0 * dst_ratio)
                left = (w0 - new_w0) // 2
                img = img.crop((left, 0, left + new_w0, h0))
            else:
                # 源更高：裁掉上下
                new_h0 = int(w0 / dst_ratio)
                top = (h0 - new_h0) // 2
                img = img.crop((0, top, w0, top + new_h0))

        # 现在比例匹配，resize 到精确目标尺寸
        img = img.resize((target_w, target_h), Image.LANCZOS)
        fname = f"_init_{int(time.time())}_{secrets.token_hex(3)}.png"
        dest = os.path.join(input_dir, fname)
        img.save(dest, "PNG")
        print(f"[comfyui] init image {w0}x{h0} → {target_w}x{target_h} "
              f"(cover-crop, ratio src={src_ratio:.2f} dst={dst_ratio:.2f})",
              file=sys.stderr)
        return fname

    # 不需要缩 → 直接 copy
    ext = os.path.splitext(abs_path)[1] or ".png"
    fname = f"_init_{int(time.time())}_{secrets.token_hex(3)}{ext}"
    dest = os.path.join(input_dir, fname)
    shutil.copy2(abs_path, dest)
    return fname


# 人像关键词：booru 数量标签优先（最强信号）+ 明确人体部位/视角
# 避免用 sitting/standing/lying 这种泛姿势词（猫狗物品也用）
HUMAN_KEYWORDS = (
    # booru 数量标签（最强）
    "1girl", "1boy", "2girls", "2boys", "multiple_girls", "multiple_boys",
    "1woman", "1man",
    # 明确人体词
    "selfie", "portrait", "looking at viewer",
    "face focus", "breast focus", "ass focus", "crotch focus", "leg focus",
    "full body", "cowboy shot", "upper body", "mirror selfie",
    # 明确人体动作 phrase（不要单独 sitting/standing）
    "1girl sitting", "1girl standing", "1girl lying", "spread legs",
    "lifting skirt", "blowjob", "kneeling blowjob",
    # 中文人物词
    "自拍", "全身照", "半身照", "正面照", "侧脸", "脸部", "镜子前", "镜中",
    "人像", "肖像",
)
COUNT_KEYWORDS = ("1girl", "2girls", "3girls", "multiple_girls", "multiple girls",
                  "1boy", "2boys", "multiple_boys", "1woman", "2women",
                  "no humans", "no_humans", "group", "crowd")


def generate(bot_id: str, prompt: str, global_cfg: dict, bot_cfg: dict = None,
             out_path: str = None, negative_extra: str = "", size: str = DEFAULT_SIZE,
             init_image: str = None, denoise: float = None) -> str:
    """出一张图，返回图片路径；失败抛 ComfyError。"""
    bot_cfg = bot_cfg or {}
    # 没显式传 init_image / denoise → 从 bot yml 读 anchor 字段，透明走 img2img
    if init_image is None:
        ai = str(bot_cfg.get("anchor_image") or "").strip()
        if ai and os.path.exists(ai):
            init_image = ai
    if denoise is None and init_image and bot_cfg.get("anchor_denoise") is not None:
        try:
            denoise = float(bot_cfg["anchor_denoise"])
        except (TypeError, ValueError):
            print("[comfyui] anchor_denoise 不是数字，按 workflow 默认", file=sys.stderr)

    # 自动拼 yml.face_traits（仅当 prompt 涉及人像时；美食/风景/物品不拼）
    prompt_lower = prompt.lower()
    has_human = any(k.lower() in prompt_lower for k in HUMAN_KEYWORDS)
    face_traits = str(bot_cfg.get("face_traits") or "").strip()
    if face_traits and has_human and face_traits not in prompt:
        prompt = f"{face_traits}, {prompt}"
        print(f"[comfyui] 拼 face_traits（含人像）: {face_traits[:60]}...", file=sys.stderr)
    elif face_traits and not has_human:
        print("[comfyui] 跳过 face_traits（非人像场景）", file=sys.stderr)

    # 数量限定词检测：人像 prompt 没数量词时仅打 warning 不强制注入
    # （强制注入会破坏多人图场景，让 LLM/用户自己显式写 1girl/2girls/multiple_girls）
    if has_human and not any(k.lower() in prompt_lower for k in COUNT_KEYWORDS):
        print("[comfyui] ⚠️ 人像 prompt 未含数量词（1girl/2girls 等）"
              "——横画面易出双胞胎，建议 LLM 显式写明", file=sys.stderr)

    img_cfg = (global_cfg.get("moments", {}) or {}).get("image_generation", {}) or {}
    cfy = img_cfg.get("comfyui", {}) or {}
    comfyui_url = cfy.get("url", "http://127.0.0.1:8188")
    output_dir = os.path.expanduser(cfy.get("output_dir") or "")
    if not output_dir:
        raise ComfyError("全局配置里没写 comfyui.output_dir")
    # ComfyUI 的 input 目录：优先配置 input_dir，否则取 output_dir 的同级 input（ComfyUI 标准布局）
    input_dir = os.path.expanduser(cfy.get("input_dir") or "") or os.path.join(os.path.dirname(output_dir), "input")

    # 有 init_image 且 workflow_img2img 配了 → 走 img2img；否则走 txt2img
    img2img_path = os.path.expanduser(cfy.get("workflow_img2img") or "")
    use_img2img = bool(init_image and os.path.exists(init_image) and img2img_path and os.path.exists(img2img_path))
    workflow_path = img2img_path if use_img2img else os.path.expanduser(cfy.get("workflow") or "")
    if not workflow_path or not os.path.exists(workflow_path):
        raise ComfyError(f"workflow 不存在: {workflow_path}")
    print(f"[comfyui] workflow={'img2img' if use_img2img else 'txt2img'}: {workflow_path}",
          file=sys.stderr)

    pos_prefix = (img_cfg.get("positive_prefix") or "").strip()
    neg_prefix = (img_cfg.get("negative_prefix") or "").strip()
    neg = (neg_prefix + ", " + negative_extra).strip(", ") if negative_extra else neg_prefix
    full_pos = (pos_prefix + ", " + prompt).strip(", ") if pos_prefix else prompt

    with open(workflow_path, encoding="utf-8") as f:
        workflow = json.load(f)

    # img2img: 把 anchor 按 size 预设等比缩放，控制输出尺寸=控制速度
    target_size = SIZE_PRESETS.get(size, SIZE_PRESETS[DEFAULT_SIZE]) if use_img2img else None
    init_image_name = _stage_init_image(init_image, input_dir, target_size) if use_img2img else None
    try:
        wf = patch_workflow(
            workflow, full_pos, neg,
            cfy.get("prompt_placeholder", "%prompt%"),
            cfy.get("negative_placeholder", "%negative_prompt%"),
            size=size,
            init_image_name=init_image_name,
            denoise=denoise,
        )
        try:
            pid = submit(comfyui_url, wf)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise ComfyError(f"连不上 ComfyUI（{comfyui_url}）: {getattr(e, 'reason', e)}")
        print(f"[comfyui] submitted prompt_id={pid}", file=sys.stderr)

        entry = wait_done(comfyui_url, pid)
        src = find_output_image(entry, output_dir)
        if not src:
            raise ComfyError("找不到输出图")

        if out_path is None:
            save_dir = os.path.expanduser(f"~/resource/media/{bot_id}/comfyui")
            os.makedirs(save_dir, exist_ok=True)
            out_path = os.path.join(save_dir, f"comfyui_{int(time.time())}_{secrets.token_hex(4)}.png")
        shutil.copy2(src, out_path)
        return out_path
    finally:
        # 本次放进 ComfyUI input 目录的起点图用完即删，不留残留
        if init_image_name:
            try:
                os.remove(os.path.join(input_dir, init_image_name))
            except OSError:
                pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("bot_id")
    p.add_argument("prompt")
    p.add_argument("--out", help="输出路径，留空自动")
    p.add_argument("--neg", default="", help="额外负向提示词")
    p.add_argument("--size", default=DEFAULT_SIZE,
                   choices=list(SIZE_PRESETS.keys()),
                   help=f"尺寸预设 (默认 {DEFAULT_SIZE})")
    p.add_argument("--init-image", help="img2img 起点图绝对路径（需切到 img2img workflow）")
    p.add_argument("--denoise", type=float,
                   help="img2img denoise (0.3-0.7 保参考图特征；不传按 workflow 默认 0.55)")
    p.add_argument("--global", dest="global_cfg",
                   help="全局配置 _global.yml 的路径；不给就按 CLAUDEBOT_GLOBAL_CFG、$CLAUDEBOTLIFE_REPO/configs/ 找")
    p.add_argument("--bot-config", help="这只 bot 的 yml（读 face_traits、anchor_image、anchor_denoise）；不给就按 $CLAUDEBOTLIFE_REPO/configs/<bot_id>.yml 找")
    args = p.parse_args()

    try:
        if not valid_bot_id(args.bot_id):
            raise ComfyError(f"bot 编号不合规: {args.bot_id!r}")
        gpath = resolve_global_cfg(args.global_cfg)
        if not gpath:
            raise ComfyError("找不到全局配置：用 --global 指定，或设 CLAUDEBOT_GLOBAL_CFG / CLAUDEBOTLIFE_REPO")
        if not os.path.exists(gpath):
            raise ComfyError(f"全局配置不存在: {gpath}")
        bot_cfg = {}
        bpath = resolve_bot_cfg(args.bot_config, args.bot_id)
        if bpath:
            try:
                bot_cfg = _load_yaml(bpath)
            except Exception as e:
                print(f"[comfyui] 读 bot yml 失败（继续，不用锚点）: {e}", file=sys.stderr)
        out = generate(args.bot_id, args.prompt, _load_yaml(gpath), bot_cfg, args.out, args.neg, args.size,
                       init_image=args.init_image, denoise=args.denoise)
    except ComfyError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"MEDIA: {out}")


if __name__ == "__main__":
    main()
