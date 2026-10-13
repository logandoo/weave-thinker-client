# -*- coding: utf-8 -*-
"""统一 license 头（Apache-2.0 SPDX 短头）—— 只作用于 opensource/ 副本（幂等）。"""
import os
import sys

DST = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'opensource')
YEAR = '2026'
HOLDER = 'Weave Thinker Contributors'

HASH_EXTS = {'.py', '.sh', '.bash', '.toml', '.txt', '.cnf', '.cfg', '.pro',
             '.properties', '.yaml', '.yml', '.tcl', '.pl', '.r'}
SLASH_EXTS = {'.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.java', '.gradle',
              '.kt', '.go', '.rs', '.php'}
BLOCK_EXTS = {'.css', '.scss'}
HTML_EXTS = {'.vue', '.html', '.xml', '.svg', '.md', '.rst'}
SKIP_EXTS = {'.json', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.pdf',
             '.mp4', '.webm', '.woff', '.woff2', '.ttf', '.eot', '.zip', '.apk',
             '.jks', '.keystore', '.xsl', '.class', '.dex', '.aar', '.pem',
             '.lock', '.lockb', '.jar', '.b64', '.map', '.webmanifest'}
SKIP_DIRS = {'node_modules', 'dist', 'static', 'test-results', 'build', '.gradle',
             'audio_files', 'output_files', 'skins_custom',
             'user_workspaces', '__pycache__', 'vendor_js'}

def headers(style):
    if style == '#':
        return [f'# Copyright (c) {YEAR} {HOLDER}',
                '# SPDX-License-Identifier: Apache-2.0', '']
    if style == '//':
        return [f'// Copyright (c) {YEAR} {HOLDER}',
                '// SPDX-License-Identifier: Apache-2.0', '']
    if style == '/*':
        return [f'/* Copyright (c) {YEAR} {HOLDER}',
                ' * SPDX-License-Identifier: Apache-2.0', ' */', '']
    return [f'<!-- Copyright (c) {YEAR} {HOLDER} -->',
            '<!-- SPDX-License-Identifier: Apache-2.0 -->', '']

def style_of(path):
    base = os.path.basename(path).lower()
    if os.path.splitext(base)[1] == '' and base in ('gradlew',):
        ext = '.sh'
    elif base.endswith(('.toml.example', '.conf.example', '.cfg.example')):
        ext = '.toml'
    else:
        ext = os.path.splitext(base)[1].lower()
    if ext in HASH_EXTS:
        return '#'
    if ext in SLASH_EXTS:
        return '//'
    if ext in BLOCK_EXTS:
        return '/*'
    if ext in HTML_EXTS:
        return 'html'
    return None

def has_spdx(text):
    # 2026-09-19 发布波修复（A4.9 I1 复审收紧）：只认「文件头」位置的 SPDX 头——
    # ①前 400 字符；②YAML frontmatter 之后的 400 字符窗口（本工具对带 frontmatter
    # 的文件把 SPDX 头插在 frontmatter 之后）。原实现只查①，导致超长 frontmatter
    # 的 SKILL.md 每轮重复插头（幂等性破坏）；纯全文检查则会把正文深处的标记串
    # 误判为已有头（合规头被静默跳过）。正文提及不等于头。
    if 'SPDX-License-Identifier' in text[:400]:
        return True
    if text.startswith('---\n') or text.startswith('---\r\n'):
        fm_end = text.find('\n---', 4)
        if fm_end > 0:
            return 'SPDX-License-Identifier' in text[fm_end:fm_end + 400]
    return False

def main():
    stats = {'added': 0, 'already': 0, 'binary': 0, 'skipped_ext': 0, 'no_style': 0,
             'skipped_frontmatter': 0}
    nostyle = []
    for base, dirs, files in os.walk(DST):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            p = os.path.join(base, fn)
            rel = os.path.relpath(p, DST)
            ext = os.path.splitext(fn)[1].lower()
            if ext in SKIP_EXTS:
                stats['skipped_ext'] += 1
                continue
            style = style_of(p)
            if style is None:
                stats['no_style'] += 1
                nostyle.append(rel)
                continue
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                stats['binary'] += 1
                continue
            if has_spdx(text):
                stats['already'] += 1
                continue
            # YAML frontmatter 必须位于文件第一行（Agent Skills / Codex / opencode 硬要求）：
            #  - frontmatter 已声明 license: 的文件不再加头（如发行 skill 的 SKILL.md）；
            #  - 未声明的（如 backend/skills/*/SKILL.md）把 SPDX 头插到 frontmatter 块之后，
            #    既不破坏解析、也保留许可覆盖。
            if text.startswith('---\n') or text.startswith('---\r\n'):
                fm_end = text.find('\n---', 4)
                if fm_end > 0 and 'license:' in text[:fm_end]:
                    stats['skipped_frontmatter'] += 1
                    continue
                if fm_end > 0:
                    insert_at = text.find('\n', fm_end + 1)
                    insert_at = len(text) if insert_at < 0 else insert_at + 1
                    hd = headers(style)
                    text = text[:insert_at] + '\n' + '\n'.join(hd) + text[insert_at:]
                    with open(p, 'w', encoding='utf-8') as f:
                        f.write(text)
                    stats['added'] += 1
                    continue
                stats['skipped_frontmatter'] += 1
                continue
            lines = text.split('\n')
            start = 1 if lines and lines[0].startswith('#!') else 0
            hd = headers(style)
            new_text = '\n'.join(lines[:start] + hd + lines[start:])
            with open(p, 'w', encoding='utf-8') as f:
                f.write(new_text)
            stats['added'] += 1
    print('LICENSE HEADER STATS:', stats)
    if nostyle:
        print('no-style (未加头，需人工确认类型):')
        for n in nostyle[:30]:
            print('  ' + n)

if __name__ == '__main__':
    main()
