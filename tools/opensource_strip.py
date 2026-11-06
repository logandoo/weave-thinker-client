# -*- coding: utf-8 -*-
"""T3：构建 opensource/（剥离隐私 + 品牌脱敏）。

流程：白名单拷贝(主仓) → openextras/ 策展 overlay 覆盖 → 定向脱敏替换 →
license 头(add_license_headers.py) → 终扫断言(FORBID)。
幂等：每次先清空 opensource/。日志 tests/w36_sanitize_log.txt。

openextras/ = 发行 overlay（单一事实源）：一切需要偏离主仓的发行内容
（README/LICENSE/.gitignore/requirements 部署文档/docs 策展版/changelog.md、
品牌化代码注释、Windows 启停脚本等）都放这里，相对路径镜像。
主仓改动这些文件后不会自动反映到发行版——需重新策展 overlay。
"""
import os
import re
import shutil
import sys
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DST = os.path.join(ROOT, 'opensource')
LOG = []

# ---------------- 1) 白名单拷贝 ----------------
DENY_DIRS = {
    # 外联参考（gitlink/冻结，非本项目）
    'hermes-agent', 'opencode', 'deepseek-harness', 'codex', 'agent_skills',
    # 隐私/运行时
    'analysis',                 # 用户行为分析工作区（含真实用户数据 xlsx + vendored pylibs；2026-09-22 补堵）
    'user_workspaces', 'memory', 'tests', 'screenshots', 'test-results', 'apk',
    # 根级 output_files/ = agent durable jobs 的运行态根（config.toml
    # `[agent.durable_jobs] jobs_root = "output_files/jobs"`，相对仓库根）。
    # 2026-11-02（C7）：此前只排除了 backend/ 作用域的两个 output_files，
    # 漏了根级——job 的 run.sh 含 `/Users/<name>` 家目录路径，会命中下方
    # FORBID 终扫并让 strip 以 exit 2 中断（发行管线硬失败）。
    'output_files',
    '.vibeweaver', '.opencode', '.impeccable', '.agents', '.claude', '.git', '.venv', 'node_modules',
    'backend/static', 'backend/audio_files', 'backend/output_files', 'backend/app/output_files',
    'backend/Fonts',
    'backend/tests',            # 后端单测不随开源（2026-08-27 curation）
    'design',                   # 内部设计资产（logo 由 main() 定向发布到 frontend/src/）
    'main_page',                # 落地页不发（2026-08-27 curation）
    'docs/api',                 # 散装 API 分册不发（策展版整合为 docs/API.md，经 openextras）
    'docs/parallel_interjection',  # 内部 wave 设计/计划文档不发（docs 白名单仅 SKINS.md；2026-09-06 补堵子目录绕过）
    'frontend/dist', 'frontend/screenshots', 'frontend/test_screenshots', 'frontend/test-screenshots', 'frontend/test-results', 'frontend/undefined', 'frontend/tests',
    'frontend/e2e/screenshots', 'frontend/e2e/tests_batch_delete_confirm', 'frontend/e2e/tests_batch_delete_current_conversation',
    'frontend/e2e/_archive',
    'frontend/android', 'frontend/frontend',
    'webview-app/app/build', 'webview-app/.gradle', 'webview-app/build', '.pnpm-store',
    'requirement', 'frontend/e2e/fixtures',
    'design/shots', 'design/shots_v3', 'design/tests', 'design/depre','design/mockups/tests', 'tools',
    '.pytest_cache', 'backend/.pytest_cache', '.dsh-whale-assets',
    'slides', 'script', 'scripts/backups', 'scripts/screenshots_3files',
    'scripts/screenshots_dm', 'scripts/screenshots_pf',
}
FILE_DENY = {
    'backend/config.toml', 'backend/config_model.toml', 'backend/config copy.toml',
    'backend/config.ci.toml', 'backend/config_model.ci.toml',   # CI/冒烟临时配置（可能含真实连接信息）
    'backend/puppeteer-config.json',   # 测试遗留，零运行时引用
    'backend/san.cnf',                 # 无引用孤儿证书配置，不发
    'backend/API.md',                  # 策展版经 openextras/docs/API.md 落地
    'webview-app/keystore.jks',
    'backend/key.pem', 'backend/cert.pem',
    # E2E/录制遗留（2026-09-28 发布前拦获：auth_test.json 含明文 JWT，export.mjs 为本地录制脚本）
    'frontend/auth_test.json', 'frontend/export.mjs',
    'backend/scripts/repair_conv3b58af5b_rows.py',
    'playwright.8158.config.mjs', 'test_timestamp_fix.py', 'func.md', 'changelog.md',
    # 根私档/scratch
    '1.m4a', '1.pdf', '1.png', '1.pptx', '1.sql', '2026 年第二季度工作复盘 (1).md',
    '26年1-6月工作汇报(1).ppt', '26年1-6月工作汇报(1).pptx',
    '44a150ec8e0a4f8e8ea66b3473a91a32_战线推移模型指标体系.docx',
    'agent_improve.md', 'API_CALL_EXAMPLES.md', 'backend.zip', 'chatllm.log',
    'chatllm.pid', 'DATABASE_DESIGN.html', 'DESIGN.md', 'FLOW_DESIGN.html',
    'PAGE_DESIGN.html', 'PRODUCT.md', 'MODIFY.html', 'MODIFY_COMPACT.html',
    'llm_doc.md', 'llm_doc.pdf', 'loop_improve.md', 'M&D.md', 'mech.md',
    'plan.md', 'record.m4a', 'result.pdf', 'test_voice.m4a', 'update_log.md',
    'flow.md', 'requirements.txt', 'package.json', 'package-lock.json',
    'frontend/playwright.w36.config.ts', 'frontend/playwright.config.js',
    'frontend/test_marked.mjs', 'backend/config.toml.bak', 'webview-app/local.properties',
}
# 随发行分发的 Agent Skill（多 harness：Codex 读 .agents、Claude Code 读 .claude、opencode 两者皆读）；
# 例外只放行这两个子树，`.agents`/`.claude` 其余内容仍拒发。
SHIP_SKILL_DIRS = ('.agents/skills/weave-thinker-deployment/',
                   '.claude/skills/weave-thinker-deployment/')

# 各目录文件白名单（None=全部，set=仅这些）
DIR_FILE_ALLOWLIST = {
    'scripts': {'project_build.sh', 'start.sh', 'stop.sh', 'restart.sh',
                'status.sh', 'dev_frontend.sh', 'apk_generate.sh',
                'docker_build.sh', 'docker_start.sh', 'docker_stop.sh'},   # split_model_config.py 不给开源（无存量单文件用户）
    'backend/scripts': {'__init__.py', 'reset_migration.py',
                        'cleanup_hallucinated_migration.py', 'audit_system_prompt.py',
                        'backfill_citation_sanitize.py',
                        # 2026-10-31：P3 存量修复脚本，随发行（sync_capture.py
                        # docstring 引用它；自托管升级路径需要，与
                        # backfill_citation_sanitize 同类）
                        'backfill_message_tombstones.py'},
    'frontend/e2e': {'global-setup.ts', 'chat.spec.ts', 'baseline_smoke.spec.ts'},
    # ARCHITECTURE.md 为纯策展文档，只经 openextras/docs/ 落地，不走主仓拷贝
    'docs': {'SKINS.md', 'USER_MANUAL.md'},
    'design': {'logo.png', 'logo.svg', 'BACKEND_DESIGN.md', 'DATABASE_DESIGN.md',
               'FLOW_DESIGN.md', 'PAGE_DESIGN.md', 'dev_plan.md'},
}

def copy_tree():
    if os.path.exists(DST):
        shutil.rmtree(DST)
    os.makedirs(DST)
    n = 0
    for base, dirs, files in os.walk(ROOT, topdown=True):
        rel = os.path.relpath(base, ROOT)
        relp = '' if rel == '.' else rel.replace(os.sep, '/')
        # 跳过发行树本体与其原子构建中间目录（opensource / opensource.tmp）——
        # 否则原子构建会把 opensource.tmp 当成源目录拷进新树（2026-10-31 回归）。
        if relp == 'opensource' or relp.startswith('opensource.') or rel == '.':
            if relp.startswith('opensource'):
                dirs[:] = []
                continue
        # deny dirs
        keep = []
        DENY_BASE = {'node_modules', '__pycache__', '.pytest_cache', 'build', '.gradle',
                     'shots', 'shots_v3', 'venv', '.venv'}
        for d in dirs:
            rp = f'{relp}/{d}' if relp else d
            allow_skill = any(
                rp == a.rstrip('/') or a.startswith(rp + '/') or rp.startswith(a)
                for a in SHIP_SKILL_DIRS
            )
            if (rp in DENY_DIRS or d in DENY_BASE or (d == 'static' and relp)) and not allow_skill:
                if rp in DENY_DIRS:
                    LOG.append(f'DENY dir: {rp}')
                continue
            keep.append(d)
        dirs[:] = keep
        for f in files:
            rp = f'{relp}/{f}' if relp else f
            if rp == '.' or not rp:
                continue
            if rp.endswith(('.log', '.DS_Store', '.pid', '.bak')):
                continue
            if rp in FILE_DENY:
                LOG.append(f'DENY file(private/scratch): {rp}')
                continue
            if rp.startswith(('tests/', 'screenshots/', 'apk/', 'memory/',
                              'user_workspaces/', 'hermes-agent/', 'opencode/',
                              'deepseek-harness/', 'codex/', 'agent_skills/',
                              '.vibeweaver/', '.opencode/', '.impeccable/', '.git/')):
                continue
            # 运行态目录（2026-10-31 修复：曾随发行树误发 TMA 签名检查点与浏览器状态；
            # 两者在主仓均 0 跟踪，纯运行时产物）：TMA 锚点/浏览器 profile 一律不发
            if rp.startswith(('backend/tma_checkpoints/',
                              'backend/app/browser_state/',
                              'backend/audio_files/', 'backend/output_files/',
                              # 2026-11-02（C7）：根级运行态作业目录
                              'output_files/')):
                continue
            # `.agents/` / `.claude/`：仅放行随发行的 skill 子树（见 SHIP_SKILL_DIRS），其余一律拒发
            if rp.startswith(('.agents/', '.claude/')) and not any(
                    rp.startswith(prefix) for prefix in SHIP_SKILL_DIRS):
                LOG.append(f'DENY file(skill dir non-allow): {rp}')
                continue
            # backend/agent_memories/：随仓发布系统文档 changelog.md（2026-09-06 由 func.md 更名）
            # + changelog 引用的产品截图 screenshots/func_doc/*.png（2026-09-15；相对路径
            #   从 changelog 所在目录解析，GitHub 上直接可看）；其余运行时数据不入库
            if rp == 'backend/agent_memories/changelog.md':
                pass
            elif rp.startswith('backend/agent_memories/screenshots/func_doc/') and rp.endswith('.png'):
                pass
            elif rp.startswith('backend/agent_memories/') or rp.startswith('backend/static/'):
                continue
            # 根目录：仅允许少量文件
            if '/' not in rp:
                allowed_root = {'README.md', '.gitignore',
                                'Dockerfile', 'docker-compose.yml', '.dockerignore'}
                if rp not in allowed_root:
                    LOG.append(f'DENY file(root scratch): {rp}')
                    continue
            # 目录白名单
            parent = os.path.dirname(rp)
            if parent in DIR_FILE_ALLOWLIST:
                if f not in DIR_FILE_ALLOWLIST[parent]:
                    LOG.append(f'DENY file(not in allowlist {parent}): {rp}')
                    continue
                    # design/ 只允许 logo/mockups/UI 子树 + 顶层白名单文件
            if rp.startswith('design/'):
                top_ok = parent == 'design' and f in DIR_FILE_ALLOWLIST.get('design', set())
                sub_ok = (rp.startswith('design/logo/') or rp.startswith('design/mockups/')
                          or rp.startswith('design/UI/'))
                if not (top_ok or sub_ok):
                    LOG.append(f'DENY file(design non-allow): {rp}')
                    continue
            # 前端杂项（test-* 与 wNN 波次脚本）
            if parent == 'frontend' and f.endswith(('.cjs', '.mjs')):
                LOG.append(f'DENY file(frontend scratch): {rp}')
                continue
            dstp = os.path.join(DST, rp)
            os.makedirs(os.path.dirname(dstp), exist_ok=True)
            shutil.copy2(os.path.join(ROOT, rp), dstp)
            n += 1
    LOG.append(f'COPIED {n} files')
    return n

# ---------------- 2) 定向脱敏 ----------------
TARGETED = [
    # main_page：服务器 URL
    ('https://<cloud-host>:8158/app/frontend/login', '<YOUR_SERVER_URL (示例: https://your-domain:8158)/app/frontend/login>'),
    ('https://<cloud-host>:8158', '<YOUR_SERVER_URL>'),
    # apk 脚本默认
    ('SERVER_URL="${1:-https://<cloud-host>:8158}"', 'SERVER_URL="${1:-https://your-server.example:8158}"'),
    # webview build.gradle dev 默认
    ("'https://192.168.31.201:8158'", "'https://your-server.example:8158'"),
    ('<domain includeSubdomains=\"true\">192.168.31.201</domain>', '<domain includeSubdomains=\"true\">your-server.example</domain> <!-- 部署时改为你的服务器域名/IP -->'),
    ('IP.2 = 192.168.31.8\n', ''),
    ('Qwen3.8(VLLM): xhigh/medium/low/关闭', 'Qwen3.8(Local): xhigh/medium/low/关闭'),
    ('Qwen3.8(VLLM)', 'Qwen3.8(Local)'),
    ('Qwen3.8 (VLLM)', 'Qwen3.8(Local)'),
    ('[legal-remediation-plan.md](legal-remediation-plan.md)', '维护者内部文档（不随开源发行；商业/依赖处置事宜请联系 alice@126.com）'),
    # 注意：build.gradle 的 keystore 口令保持 weavernote 原样——apk_generate.sh
    # 自动生成的开发 keystore 即用该口令，两侧必须一致（勿打 CHANGE_ME）。
    ('/home/user/Documents/DEV/chatbot/.venv/bin/python', '<project-root>/.venv/bin/python'),
    # vibeweaver 镜像路径（2026-10-31 刷新后 UPSTREAM.md 用绝对路径）→ 家目录相对形式
    ('/home/user/Documents/DEV/SKILLS/vibeweaver', '~/Documents/DEV/SKILLS/vibeweaver'),
    # alice 实证注释 → 泛化
    ('（60 服务器 alice 实证：夜校/血吸虫/运维 07-29 导入后从未再召回）', '（生产环境实证：低频主题导入后未被召回）'),
    ('根因（60 服务器 alice 实证）：', '根因（生产环境实证）：'),
    ('覆盖三个根因（60 服务器 alice 实证事故）：', '覆盖三个根因（生产环境实证事故）：'),
    ('（A4.6 根因 2：alice 6 active < 10 阈值，旧代码永远跳过）。', '（A4.6 根因 2：低活跃用户 6 active < 10 阈值，旧代码永远跳过）。'),
    ('（实测 alice 返回', '（实测低活跃用户返回'),
    # 内部服务器昵称 → 泛化（2026-09-12 A4.9 M1：config.py/voice_service.py 残留）
    ('60 服务器', '生产环境'),
    ('60 生产事故', '生产事故'),
    ('上线 60 前', '上线生产前'),
    ('工单 §', '规格 §'),
    # biklimax 注释 → 泛化
    ('on the user\'s custom provider (biklimax.cn),', "on the user's custom provider,"),
    ('— biklimax.cn —', '— user custom provider —'),
    # pdf_exporter 会话 ID 注释
    ('# 2fa87be4 (罗航 Agent 评测与推广策略).', '# 长文档 PDF 导出回归用例。'),
    # 品牌（定向优先）
    ('Weave Thinker (罗航)', 'Weave Thinker'),
    ('Weave Thinker（罗航）', 'Weave Thinker'),
    # vibeweaver skill 文档示例口令 → 占位（2026-10-01 集成波：示例口令
    # 命中 FORBID 密钥形态，发行版一律占位）
    ('8i9o0p-[=]', '<EXAMPLE-PASSWORD>'),
    ('8i9o0p', '<EXAMPLE-PASSWORD>'),
    ('罗航 · Weave Thinker', 'Weave Thinker'),
    ('「罗航 / Weave Thinker」', '「Weave Thinker」'),
    ('罗航 / Weave Thinker', 'Weave Thinker'),
    ('罗航/Weave Thinker', 'Weave Thinker'),
    ('罗航 Weave Thinker', 'Weave Thinker'),
    ('（罗航）', ''),
    ('"system": "Weave Thinker (罗航)"', '"system": "Weave Thinker"'),
    # persona 行（保持中文通顺）
    ('你是罗航的AI助手，一个带工具调用', '你是 Weave Thinker，用户的 AI 助手，一个带工具调用'),
    ('默认身份是\\u201c罗航\\u201d，是用户的AI助手。', '默认身份是\\u201cWeave Thinker\\u201d，是用户的AI助手。'),
    ('默认身份是“罗航”，是用户的AI助手。', '默认身份是“Weave Thinker”，是用户的AI助手。'),
    ('is "罗航" and must not be invented or overridden.', 'is "Weave Thinker" and must not be invented or overridden.'),
    ('默认称呼为\\u201c罗航\\u201d。', '默认称呼为“Weave Thinker”。'),
    ('就以\\u201c罗航\\u201d自称。', '就以“Weave Thinker”自称。'),
    ('必须首先明确确认：是的，我就是 Weave Thinker，当前对话就运行在我身上。', '必须首先明确确认：是的，我就是 Weave Thinker，当前对话就运行在我身上。'),
    ('禁止随口编造或只给一句“我是罗航”。', '禁止随口编造或只给一句“我是 Weave Thinker”。'),
    ('禁止随口编造或只给一句"我是罗航"。', '禁止随口编造或只给一句"我是 Weave Thinker"。'),
    ('你当前运行的这个产品/系统就叫 Weave Thinker——Weave Thinker 就是你自己。', '你当前运行的这个产品/系统就叫 Weave Thinker——Weave Thinker 就是你自己。'),
    ('为AI助手\\u201c罗航\\u201d工作。', '为 AI 助手 Weave Thinker 工作。'),
    ("没有时就以'罗航'自称。", "没有时就以'Weave Thinker'自称。"),
    ("没有用户自定义名称时就以'罗航'自称。", "没有用户自定义名称时就以'Weave Thinker'自称。"),
    ('你是AI助手“罗航”。', '你是 AI 助手 Weave Thinker。'),
    ('你是全双工语音助手“罗航”。', '你是全双工语音助手 Weave Thinker。'),
    ('你是全双工语音助手\\u201c罗航\\u201d。', '你是全双工语音助手“Weave Thinker”。'),
    ('默认人格「罗航」', '默认人格「Weave Thinker」'),
    ('content: \'罗航\';', 'content: \'WT\';'),
    ('巨型描边「罗航/航」', '巨型描边「WT」'),
    ('<h1 class="login-title">罗航</h1>', '<h1 class="login-title">Weave Thinker</h1>'),
    ('<span class="logo-text">罗航</span>', '<span class="logo-text">Weave Thinker</span>'),
    ("return '罗航'", "return 'Weave Thinker'"),
    ('确定要退出罗航吗？', '确定要退出 Weave Thinker 吗？'),
    ('<p class="empty-brand-title">罗航</p>', '<p class="empty-brand-title">Weave Thinker</p>'),
    ('<div class="nm">罗航<i>.</i></div>', '<div class="nm">Weave<i>.</i></div>'),
    ('产品名「罗航」取的是罗盘导航的意思。每一次记录和思考，都是在绘制你自己的知识地图。',
     'Weave Thinker 的名字取自「编织思维」之意：每一次记录和思考，都是在绘制你自己的知识地图。'),
    # changelog.md 混排（随仓发布的 backend/agent_memories/ 系统文档，原名 func.md）
    ('罗航（Weave Thinker）', 'Weave Thinker'),
    # SKINS.md 内部测试路径 → 泛化（发行树不含 tests/；2026-09-23 策展）
    ('（`tests/api/test_skin_api.py` + `frontend/e2e/skin_system.spec.ts` 双向断言）',
     '（开发侧双向断言保障；测试不随发行）'),
    ('（参考 `design/mockups/tests/check_contrast.mjs`）', '（开发侧有对比度检查脚本）'),
    ('`tests/w12_capture.py` 自建 DOM 审计器', '自建 DOM 审计器（开发侧脚本）'),
    ('`tests/skin_audit.py`（输出 `tests/skin_audit_report.md`，CI 化前可手工跑）',
     '皮肤审计脚本（输出报告；开发侧，测试不随发行）'),
    ('（全表 `tests/skin_audit_report.md` §3，重跑 `python3 tests/skin_audit.py` 再生）',
     '（全表见开发侧审计报告，重跑审计脚本再生）'),
    ('后端 API 测试：`tests/api/test_skin_api.py`（目录/偏好 22 断言）· `tests/api/test_skin_upload.py`（上传 23 断言）；运行需后端在位',
     '后端 API 测试（目录/偏好 22 断言 · 上传 23 断言；测试不随发行）；运行需后端在位'),
    ('（`tests/mm_ratings/`）', '（评级记录不随发行）'),
    ('`tests/w12_capture.py` DOM 审计器', '自建 DOM 审计器（开发侧脚本）'),
]

def sanitize():
    applied = {}
    for base, dirs, files in os.walk(DST):
        for f in files:
            p = os.path.join(base, f)
            rp = os.path.relpath(p, ROOT)
            if f.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico',
                           '.pdf', '.mp4', '.webm', '.woff', '.woff2', '.ttf', '.eot',
                           '.zip', '.apk', '.jks', '.keystore', '.xsl', '.class',
                           '.dex', '.aar')):
                continue
            try:
                with open(p, 'r', encoding='utf-8') as fh:
                    txt = fh.read()
            except (UnicodeDecodeError, OSError):
                LOG.append(f'SKIP(binary unreadable): {rp}')
                continue
            orig = txt
            for a, b in TARGETED:
                txt = txt.replace(a, b)
            if txt != orig:
                n = sum(orig.count(a) for a, _ in TARGETED if a in orig)
                applied[rp] = n
                with open(p, 'w', encoding='utf-8') as fh:
                    fh.write(txt)
            # 兜底：剩余 罗航
            if '罗航' in txt:
                n = txt.count('罗航')
                txt2 = txt.replace('罗航', 'Weave Thinker')
                applied[rp] = applied.get(rp, 0) + n
                with open(p, 'w', encoding='utf-8') as fh:
                    fh.write(txt2)
                txt = txt2
                LOG.append(f'SANITIZE catch-all 罗航×{n}: {rp}')
            # 兜底：catch-all 造成的品牌重名（如 <title> "Weave Thinker — Weave Thinker"）
            if 'Weave Thinker — Weave Thinker' in txt:
                txt = txt.replace('Weave Thinker — Weave Thinker', 'Weave Thinker')
                applied[rp] = applied.get(rp, 0) + 1
                with open(p, 'w', encoding='utf-8') as fh:
                    fh.write(txt)
                LOG.append(f'SANITIZE dedupe 品牌重名: {rp}')
    for k in sorted(applied):
        LOG.append(f'SANITIZE targeted/catch-all total≈{applied[k]}: {k}')

# ---------------- 3) 终扫断言 ----------------
FORBID = re.compile(
    r'罗航|罗淦|alice|60\.204\.207\.29|124\.70\.142\.2|biklimax|8i9o0p'
    # A4.9 评审 Minor#6：不再只钉本机用户名——任意家目录路径一律算泄漏
    r'|/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+'
    # 任意私网**主机**地址（末段非 0；192.168.0.0/172.16.0.0 这类 CIDR 网段
    # 字面量是代码里的合法常量，不算泄漏）
    r'|192\.168\.\d{1,3}\.[1-9]\d{0,2}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.[1-9]\d{0,2}'
    # 凭据形态（AWS / GitHub / Slack / PEM 私钥块）
    r'|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}'
    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----'
    r'|sk-[a-zA-Z0-9]{16,}|hf_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{20,}'
    r'|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'   # JWT 形态（2026-09-28 auth_test.json 泄露未遂后补堵）
    r'|(?:^|\W)user[= ]+[0-9a-f]{8,}(?:\W|$)')   # 生产用户 ID 形如 "user d2998b76"（2026-09-22 A4.9 复审 Important 补堵）
ALLOW_HINTS = {  # 文件: 允许的残留（用户指定公开内容）
    # vibeweaver MIT 署名与上游溯源 URL（2026-10-01 集成波）：MIT 许可要求
    # 保留版权署名，溯源 URL 为公开事实——键为不含 opensource/ 前缀的相对路径。
    'backend/skills/vibeweaver/LICENSE': {'alice'},
    'backend/skills/vibeweaver/UPSTREAM.md': {'alice'},
    # 2026-10-31 vibeweaver 刷新新增 NOTICE（上游 MIT 版权署名，保留为公开事实）
    'backend/skills/vibeweaver/NOTICE': {'alice'},
}

def final_scan():
    bad = []
    binary_scanned = 0
    unreadable = 0
    for base, dirs, files in os.walk(DST):
        for f in files:
            p = os.path.join(base, f)
            # 键相对 DST（ALLOW_HINTS 即 backend/... 形式）。历史实现用
            # relpath(p, ROOT) 再手工剥 'opensource/' 前缀——原子构建把 DST 换成
            # opensource.tmp 后该前缀失配、白名单整体失效（A4.9 评审 Minor#10）。
            rp = os.path.relpath(p, DST)
            try:
                with open(p, 'r', encoding='utf-8') as fh:
                    txt = fh.read()
            except UnicodeDecodeError:
                # A4.9 评审 Important#5：二进制不能静默跳过——按 latin-1 逐字节
                # 解码（ASCII 模式仍可命中），并把数量写进 CLEAN 行（诚实计数）。
                with open(p, 'rb') as fh:
                    txt = fh.read().decode('latin-1', 'ignore')
                binary_scanned += 1
            except OSError:
                unreadable += 1
                continue
            for m in FORBID.finditer(txt):
                line = txt[:m.start()].count('\n') + 1
                # 白名单：策展文档中用户指定公开的 DCO/商务联系人邮箱（文件名大小写两种形态都放行）
                if rp.lower().endswith(('contributing.md', 'readme.md', 'legal-report.md',
                                        'license-compliance.md')):
                    if 'alice@126.com' in txt and m.group(0) in ('alice', '罗淦'):
                        continue
                # 文件级允许残留（ALLOW_HINTS；键去掉 opensource/ 前缀）
                _rp_key = rp[len('opensource/'):] if rp.startswith('opensource/') else rp
                if m.group(0) in ALLOW_HINTS.get(_rp_key, ()):
                    continue
                bad.append(f'{rp}:{line}: {m.group(0)}')
    if bad:
        LOG.append('FINAL SCAN FAILED ' + str(len(bad)))
        for b in bad[:40]:
            LOG.append('  ' + b)
        print('FINAL SCAN FAILED:')
        for b in bad[:40]:
            print('  ' + b)
        sys.exit(2)
    LOG.append('FINAL SCAN CLEAN (0 hits, DCO 联系人白名单除外; '
               'binary scanned=%d, unreadable=%d)' % (binary_scanned, unreadable))

# ---------------- 4) config 示例（掩码） ----------------

SECRET_KEYS = re.compile(
    r'password|passwd|secret|api_?key|auth_?token|jwt|access_?key|credential|private_?key|hotword_secret', re.I)

def mask_value(key, val, section=''):
    if section == 'database' and key in ('name',):
        return '<YOUR-DB-NAME>'
    # P0 S2: the public template must be safe for multi-user self-hosting even
    # when the maintainer's local dev config opts into single-user bypass.
    if section == 'security' and key == 'super_admin_bypass':
        return False
    if isinstance(val, bool) or val is None:
        return val
    if re.search(r'username|user_name|^user$', key, re.I):
        return '<YOUR-DB-USER>'
    if isinstance(val, int):
        if re.search(r'port', key, re.I):
            if section == 'server':
                return 8158
            if section == 'database':
                return 5432
        return val
    if isinstance(val, (float, list, dict)):
        return val
    s = str(val)
    if re.match(r'^https?://', s):
        return 'https://your-endpoint.example/v1'
    # 本机绝对路径（如 oauth_state_file）→ 家目录占位（任意机器可复现）
    s = re.sub(r'/Users/[^/]+', '<YOUR-HOME>', s)
    if os.path.expanduser('~') not in ('/', '~'):
        s = s.replace(os.path.expanduser('~'), '<YOUR-HOME>')
    if SECRET_KEYS.search(key):
        return '<YOUR-SECRET>'
    if re.search(r'^\S+@\S+\.\S+$', s):
        return '<YOUR-EMAIL>'
    return s

def gen_config_example():
    # vendor 布局适配（W27 D-114）：上游主仓 backend/ 在本仓 [[map]] 为 server/。
    # 仅输入路径容错（输出契约 dst/backend/config.toml.example 不变）。
    _cfg_src = None
    for _rel in ('backend/config.toml', 'server/config.toml'):
        _p = os.path.join(ROOT, _rel)
        if os.path.exists(_p):
            _cfg_src = _p
            break
    if _cfg_src is None:
        _cfg_src = os.path.join(ROOT, 'backend/config.toml')
    with open(_cfg_src, 'rb') as f:
        cfg = tomllib.load(f)
    out = ['# Weave Thinker 配置模板（主基础设施配置）。',
           '# 复制为 backend/config.toml 后按部署环境填写。所有 <YOUR-*> 占位必须替换。',
           '# 模型相关配置见 config_model.toml（模板 config_model.toml.example）。',
           '']
    def emit(sec, chain, target=None):
        if target is None:
            target = out
        dotted = '.'.join(key_seg(x) for x in chain)
        target.append(f'[{dotted}]')
        for k, v in sec.items():
            if isinstance(v, dict):
                emit(v, chain + [k], target=target)
            else:
                mv = mask_value(k, v, section=chain[0])
                target.append(f'{k} = {serialize(mv)}')
        target.append('')
    def serialize(v):
        if isinstance(v, bool):
            return 'true' if v else 'false'
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            return '[' + ', '.join(serialize(x) for x in v) + ']'
        t = str(v)
        if '\n' in t:
            return '"""' + t.replace('\\', '\\\\').replace('"', '\\"') + '"""'
        return '"' + t.replace('\\', '\\\\').replace('"', '\\"') + '"'
    def key_seg(k):
        if k and k.replace('_', '').replace('-', '').isalnum():
            return k
        return '"' + k + '"'
    for k, v in cfg.items():
        emit(v if isinstance(v, dict) else {}, [k])
    with open(os.path.join(DST, 'backend/config.toml.example'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))
    for d in ('config.toml', 'config_model.toml'):
        p = os.path.join(DST, 'backend', d)
        if os.path.exists(p):
            os.remove(p)
    # config_model.example：若已拷贝且干净，保留；否则同样掩码生成
    pm = os.path.join(DST, 'backend/config_model.toml.example')
    if os.path.exists(pm):
        with open(pm, encoding='utf-8') as f:
            t = f.read()
        if FORBID.search(t) or 'sk-' in t:
            with open(os.path.join(ROOT, 'backend/config_model.toml'), 'rb') as f:
                cfgm = tomllib.load(f)
            out2 = ['# Weave Thinker 模型配置模板（覆盖式合并到主 config.toml；文件缺失时回退主文件）。',
                    '# 复制为 backend/config_model.toml 后填写。所有 <YOUR-*> 占位必须替换。', '']
            for k, v in cfgm.items():
                emit(v if isinstance(v, dict) else {}, [k], target=out2)
            with open(pm, 'w', encoding='utf-8') as f:
                f.write('\n'.join(out2))
            LOG.append('config_model.toml.example: 掩码重生')
        else:
            LOG.append('config_model.toml.example: 原样保留（干净）')

def curate_release_metadata():
    """发行版元数据策展：测试文件不随发（frontend/tests 拒发）→ 移除引用它们的 npm script。

    `prebuild` 是构建前 fail-closed 检查（vue-router #2704 backport），不是测试；
    把检查脚本复制到 frontend/scripts/（发行树会随发）并改写路径，保留构建门禁。
    """
    import json
    p = os.path.join(DST, 'frontend', 'package.json')
    if not os.path.exists(p):
        return
    with open(p, encoding='utf-8') as f:
        pkg = json.load(f)
    scripts = pkg.get('scripts', {})
    changed = False
    if 'test:unit' in scripts:
        del scripts['test:unit']
        changed = True
        LOG.append('CURATE frontend/package.json: 移除 test:unit（测试文件不随发）')
    prebuild = str(scripts.get('prebuild', '') or '')
    if 'tests/' in prebuild:
        src = os.path.join(ROOT, 'frontend', 'tests', 'workflows',
                           'test_vue_router_edge_backport.cjs')
        dst = os.path.join(DST, 'frontend', 'scripts',
                           'check_vue_router_backport.cjs')
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            text = open(src, encoding='utf-8').read()
            text = re.sub(
                r"const ROOT = path\.resolve\(__dirname, '\.\.', '\.\.'\);",
                "const ROOT = path.resolve(__dirname, '..');",
                text,
            )
            with open(dst, 'w', encoding='utf-8') as f:
                f.write(text)
            scripts['prebuild'] = 'node scripts/check_vue_router_backport.cjs'
            changed = True
            LOG.append('CURATE frontend/package.json: prebuild → scripts/check_vue_router_backport.cjs（随发）')
        else:
            scripts.pop('prebuild', None)
            changed = True
            LOG.append('CURATE frontend/package.json: 移除 prebuild（检查脚本缺失）')
    if changed:
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(pkg, f, ensure_ascii=False, indent=2)
            f.write('\n')

def _build():
    copy_tree()
    # 策展文件（LICENSE/README/CONTRIBUTING/.gitignore/requirements/legal-report）
    EX = os.path.join(ROOT, 'tools', 'openextras')
    for base, dirs, files in os.walk(EX):
        for f in files:
            rel = os.path.relpath(os.path.join(base, f), EX)
            dstp = os.path.join(DST, rel)
            os.makedirs(os.path.dirname(dstp), exist_ok=True)
            shutil.copy2(os.path.join(base, f), dstp)
            LOG.append(f'EXTRA: {rel}')
    sanitize()
    gen_config_example()
    curate_release_metadata()
    # 脱敏版 AGENTS.md（源在 tools/open_AGENTS.md）
    if os.path.exists(os.path.join(ROOT, 'tools', 'open_AGENTS.md')):
        shutil.copy(os.path.join(ROOT, 'tools', 'open_AGENTS.md'),
                    os.path.join(DST, 'AGENTS.md'))
        LOG.append('AGENTS.md: 脱敏版覆盖')
    # logo 定向发布到 frontend/src/（README 首图引用该路径；源在 design/ 与 frontend/public/）
    os.makedirs(os.path.join(DST, 'frontend', 'src'), exist_ok=True)
    for src, dst in ((os.path.join(ROOT, 'design', 'logo.png'),
                     os.path.join(DST, 'frontend', 'src', 'logo.png')),
                    (os.path.join(ROOT, 'frontend', 'public', 'icon.svg'),
                     os.path.join(DST, 'frontend', 'src', 'logo.svg'))):
        if os.path.exists(src):
            shutil.copy2(src, dst)
            LOG.append(f'LOGO: {os.path.relpath(src, ROOT)} -> {os.path.relpath(dst, DST)}')
        else:
            LOG.append(f'WARN logo source missing: {src}')
    # 统一 license 头（Apache-2.0 SPDX，幂等；作用于 DST 副本）
    import subprocess
    _hdr_tool = os.path.join(ROOT, 'tools', 'add_license_headers.py')
    hr = subprocess.run([sys.executable, _hdr_tool, DST],
                       capture_output=True, text=True)
    # 2026-10-31 修复：补头曾 fail-open（只记录 stderr）——半成品树（文件已拷、
    # 头未补）会被当成成功产物发布。fail-closed：非 0 立刻中止。
    if hr.returncode != 0:
        print('HEADERS FAILED rc=%s\n%s' % (hr.returncode, hr.stderr[-2000:]))
        sys.exit(3)
    # A4.9 评审 Important#2：补头工具自身没有失败信号（不可读文件吞成 binary），
    # 故显式记录 STATS 行，并跑只读 --check 断言「零缺头」——added=0/517 可区分。
    _stats_line = next((ln for ln in hr.stdout.splitlines()
                        if ln.startswith('LICENSE HEADER STATS:')), '')
    LOG.append('HEADERS: ' + (_stats_line or 'ok'))
    hc = subprocess.run([sys.executable, _hdr_tool, '--check', DST],
                        capture_output=True, text=True)
    if hc.returncode != 0:
        print('HEADERS CHECK FAILED rc=%s\n%s\n%s'
              % (hc.returncode, hc.stdout[-2000:], hc.stderr[-1000:]))
        sys.exit(3)
    LOG.append('HEADERS-CHECK: ' + next(
        (ln for ln in hc.stdout.splitlines() if ln.startswith('LICENSE HEADER CHECK:')), 'ok'))
    # License headers change the pinned checker bytes. The distribution must
    # carry a pin for its OWN checker, otherwise the production gate fails
    # closed on every write after opensource_strip.py.
    import hashlib
    _checker = os.path.join(DST, 'backend', 'skills', 'vibeweaver', 'scripts', 'assert_artifacts.py')
    _pin = os.path.join(DST, 'backend', 'skills', 'vibeweaver', 'scripts', 'assert_artifacts.sha256')
    if os.path.isfile(_checker):
        with open(_checker, 'rb') as _f:
            _digest = hashlib.sha256(_f.read()).hexdigest()
        with open(_pin, 'w', encoding='utf-8') as _f:
            _f.write(_digest + '\n')
        LOG.append('PIN: refreshed post-header checker pin ' + _digest[:12])
    else:
        # A4.9 评审 Minor#9：checker 缺失=发行树坏了（自托管侧 gate 会因无 pin
        # 拦掉所有写入），不能当可选步骤静默通过。
        print('PIN FAILED: checker missing at ' + _checker)
        sys.exit(4)
    # 2026-10-31 修复：终扫必须在**所有**写盘步骤之后（AGENTS.md 覆盖/logo/补头）
    # ——原位置在覆盖 AGENTS.md 之前，open_AGENTS.md 的泄漏永远不会被扫到。
    final_scan()
    with open(os.path.join(ROOT, 'tests', 'w36_sanitize_log.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(LOG))
    print('\n'.join(LOG[-25:]))
    print('OK: opensource/ built')


def main():
    """原子构建（2026-10-31 A4.9 评审 Important#3）：先建到 `opensource.tmp/`，
    全部步骤成功后再整目录换入。中途被 SIGKILL / 工具超时杀死时，旧 opensource/
    原样保留——不再产生「已 wipe 的半成品树」被误提交（2026-10-31 事故形态）。

    并发锁（2026-10-31 用户关切）：多个 agent 会话可能同时在本仓跑 strip——
    两个构建共用 opensource.tmp 会互相踩踏。`flock` 非阻塞独占，第二个进程直接
    报错退出（exit 5），绝不半途换入。"""
    global DST
    final_dst = DST
    tmp_dst = final_dst.rstrip('/\\') + '.tmp'
    lock_path = final_dst.rstrip('/\\') + '.lock'
    try:
        import fcntl
        _lf = open(lock_path, 'w')
        try:
            fcntl.flock(_lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print('ABORT: another opensource build is running (lock: %s)' % lock_path)
            _lf.close()
            sys.exit(5)
    except ImportError:  # 非 POSIX（Windows 开发机）：退化为无锁
        _lf = None
    try:
        shutil.rmtree(tmp_dst, ignore_errors=True)
        DST = tmp_dst
        try:
            _build()
        except BaseException:
            shutil.rmtree(tmp_dst, ignore_errors=True)
            DST = final_dst
            raise
        shutil.rmtree(final_dst, ignore_errors=True)
        os.replace(tmp_dst, final_dst)
        DST = final_dst
    finally:
        if _lf is not None:
            try:
                import fcntl
                fcntl.flock(_lf.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            _lf.close()


if __name__ == '__main__':
    main()
