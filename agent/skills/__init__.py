"""Skill 文件夹系统 — 每个 skill 是一个文件夹，包含 SKILL.md + references/ + gotchas.md.

注册表由 planner.build_registry() 在启动时扫描目录构建，
此处不维护硬编码列表（避免与实际目录漂移）。
"""
