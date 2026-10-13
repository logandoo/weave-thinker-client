#!/usr/bin/env python3
"""把既有部署的 config.toml / config_model.toml 导入本地栈（测试/迁移用）。

安全约定：
- 只覆盖 [server] 与 [database] 两段（指向本地 PG/端口），其余内容原样保留（含密钥）。
- 目标文件 0600；不打印任何键值（仅段级确认）。
- 目标文件位于 gitignore 覆盖路径（server/、LOCALSTACK_HOME/app）。

用法：
  python3 localstack/import_config.py \
      --source-config /path/to/config.toml \
      --source-model  /path/to/config_model.toml \
      --target server            # 或 LOCALSTACK_HOME/app
      [--force]                  # 目标已存在时先备份再覆盖（默认存在即跳过）
"""
import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _set_kv(section: str, key: str, value: str, text: str) -> str:
    pat = rf'(?m)^({key}\s*=).*$'
    idx = text.index(f'[{section}]')
    head, tail = text[:idx], text[idx:]
    out, n = re.subn(pat, rf'\1 {value}', tail, count=1)
    if n == 0:
        raise SystemExit(f'{section}.{key} not found in source config')
    return head + out


_TEMPLATE = Path(__file__).resolve().parent.parent / 'server' / 'config.toml.example'
# W12：模板补齐白名单——vibeweaver 集成段的默认值（force_inject 等）随导入生效；
# 用户既有值一律保留（仅在段/键缺失时补齐）。
_MERGE_SECTIONS = ('agent.skills', 'hooks', 'vibeweaver')


def _section_block(template_text: str, section: str) -> str:
    head = f'[{section}]'
    idx = template_text.index(head)
    end = template_text.find('\n[', idx + len(head))
    block = template_text[idx:end if end != -1 else len(template_text)]
    return block.rstrip('\n') + '\n'


def _merge_section(text: str, template_text: str, section: str) -> str:
    """段缺失→整段追加；段存在→逐键补齐（绝不覆盖既有键值）。"""
    try:
        block = _section_block(template_text, section)
    except ValueError:
        return text
    head = f'[{section}]'
    # 评审修复：段存在性判定必须行锚定（值/多行字符串中含同名字面量不得误判）。
    m_head = re.search(rf'(?m)^\[{re.escape(section)}\]\s*(?:#.*)?$', text)
    if not m_head:
        if not text.endswith('\n'):
            text += '\n'
        return text + '\n' + block
    idx = m_head.start()
    line_end = text.find('\n', idx)
    insert_at = line_end + 1 if line_end != -1 else len(text)
    m_end = re.search(r'(?m)^\[', text[idx + len(head):])
    sec_end = idx + len(head) + (m_end.start() if m_end else len(text) - idx - len(head))
    section_text = text[idx:sec_end]
    add = []
    for ln in block.splitlines()[1:]:
        m = re.match(r'^(\s*)([A-Za-z0-9_.-]+)\s*=', ln)
        if not m:
            continue
        key = m.group(2)
        if not re.search(rf'(?m)^\s*{re.escape(key)}\s*=', section_text):
            add.append(ln)
    if add:
        text = text[:insert_at] + '\n'.join(add) + '\n' + text[insert_at:]
    return text


def _write_secure(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    path.chmod(0o600)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source-config', required=True)
    ap.add_argument('--source-model', required=True)
    ap.add_argument('--target', required=True, help='目标目录（如 server 或 LOCALSTACK_HOME/app）')
    ap.add_argument('--force', action='store_true', help='覆盖已存在目标（先备份）')
    args = ap.parse_args()

    src_main = Path(args.source_config).expanduser().resolve()
    src_model = Path(args.source_model).expanduser().resolve()
    target = Path(args.target).expanduser()
    if not target.is_absolute():
        target = (ROOT / target).resolve()

    for p in (src_main, src_model):
        if not p.exists():
            print(f'missing source: {p}', file=sys.stderr)
            return 1

    dst_main = target / 'config.toml'
    dst_model = target / 'config_model.toml'
    if dst_main.exists() and not args.force:
        print(f'{dst_main} exists — skip（--force 覆盖，先备份）')
        return 0

    text = src_main.read_text(encoding='utf-8')
    pg_port = int(os.environ.get('LOCALSTACK_PG_PORT', 55432))
    backend_port = int(os.environ.get('LOCALSTACK_BACKEND_PORT', 8159))
    text = _set_kv('server', 'host', '"127.0.0.1"', text)
    text = _set_kv('server', 'port', str(backend_port), text)
    text = _set_kv('database', 'host', '"127.0.0.1"', text)
    text = _set_kv('database', 'port', str(pg_port), text)
    text = _set_kv('database', 'username', '"postgres"', text)
    text = _set_kv('database', 'password', '""', text)
    text = _set_kv('database', 'name', '"weavethinker"', text)
    # 工作区根目录固定到 LOCALSTACK_HOME（绝对路径），避免随 CWD 解析而污染仓库。
    # [workspace] 段缺失时优雅补段；段存在但无 root_dir 时在段头后插入，绝不产生重复段。
    home = Path(os.environ.get('LOCALSTACK_HOME', ROOT / '.localstack')).resolve()
    ws_value = f'"{(home / "workspaces").as_posix()}"'
    if '[workspace]' in text:
        try:
            text = _set_kv('workspace', 'root_dir', ws_value, text)
        except SystemExit:
            idx = text.index('[workspace]')
            line_end = text.find('\n', idx)
            if line_end == -1:
                text = text + f'\nroot_dir = {ws_value}\n'
            else:
                text = text[:line_end + 1] + f'root_dir = {ws_value}\n' + text[line_end + 1:]
    else:
        text += f'\n[workspace]\nroot_dir = {ws_value}\n'

    # W12 P0#4：模板补齐 vibeweaver 集成段（缺失段/键；用户既有值保留）。
    try:
        template_text = _TEMPLATE.read_text(encoding='utf-8')
        for section in _MERGE_SECTIONS:
            text = _merge_section(text, template_text, section)
    except OSError:
        pass

    ts = time.strftime('%Y%m%d_%H%M%S')
    backup_dir = target / '.backups'
    for dst in (dst_main, dst_model):
        if dst.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f'{dst.name}.bak.{ts}'
            shutil.copy2(dst, backup)
            print(f'backup: {backup.relative_to(target)}')

    _write_secure(dst_main, text)
    _write_secure(dst_model, src_model.read_text(encoding='utf-8'))
    print(f'imported → {target}（[server]/[database] 已本地化；模型配置原样；文件 0600）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
