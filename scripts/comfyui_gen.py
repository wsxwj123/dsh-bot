#!/usr/bin/env python3
"""旧位置的兼容入口。ComfyUI 生图脚本已经搬到 skills/comfyui-skill/scripts/comfyui_gen.py，
两种底层共用那一份（约定 v1.1 5.21）；老的技能说明还在调这里时，原样转过去。"""
import os
import runpy
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CLAUDEBOTLIFE_REPO", REPO)
target = os.path.join(REPO, "skills", "comfyui-skill", "scripts", "comfyui_gen.py")
sys.argv[0] = target
runpy.run_path(target, run_name="__main__")
