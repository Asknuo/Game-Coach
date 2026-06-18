"""Skill 文件夹系统 — 每个 skill 是一个文件夹，包含 SKILL.md + references/ + gotchas.md."""

import os

SKILLS_DIR = os.path.dirname(__file__)

SKILL_FOLDERS = [
    "survival",
    "dragon",
    "laning",
    "build",
    "macro",
    "teamfight",
    "review",
]

__all__ = ["SKILL_FOLDERS", "load_skill", "SKILLS_DIR"]
