import { test, expect, type Page, type APIRequestContext, type JSHandle } from '@playwright/test';

const APP = '/app/frontend';
const createdIds: string[] = [];

async function login(page: Page) {
  await page.goto(APP + '/login');
  await page.waitForSelector('#username, .new-chat-btn', { timeout: 30000 });
  if (await page.locator('#username').count()) {
    await page.fill('#username', 'test');
    await page.fill('#password', '123456');
    await page.click('.login-btn');
  }
  await expect(page.locator('.new-chat-btn')).toBeVisible({ timeout: 30000 });
}

async function createConversation(request: APIRequestContext, title: string) {
  const loginRes = await request.post('/api/auth/login', { data: { username: 'test', password: '123456' } });
  expect(loginRes.status()).toBe(200);
  const token = (await loginRes.json()).access_token;
  const headers = { Authorization: `Bearer ${token}` };
  const me = await request.get('/api/auth/me', { headers });
  expect((await me.json()).username).toBe('test');
  const assistantsRes = await request.get('/api/assistants', { headers });
  const assistants = await assistantsRes.json();
  const defaultAssistant = assistants.find((a: any) => a.is_default || a.name === '默认助手');
  expect(defaultAssistant?.id).toBeTruthy();
  const res = await request.post('/api/conversations', { headers, data: { title, assistant_id: defaultAssistant.id } });
  expect(res.status()).toBe(200);
  const body = await res.json();
  const id = body.id || body.conversation_id;
  expect(id).toBeTruthy();
  createdIds.push(id);
  return id as string;
}

async function cleanupCreated(request: APIRequestContext) {
  if (!createdIds.length) return;
  const loginRes = await request.post('/api/auth/login', { data: { username: 'test', password: '123456' } });
  const token = (await loginRes.json()).access_token;
  const headers = { Authorization: `Bearer ${token}` };
  for (const id of createdIds.splice(0)) await request.delete(`/api/conversations/${id}`, { headers }).catch(() => {});
}

function conversationState(id: string, title: string, overrides: Record<string, any> = {}) {
  return {
    id,
    title,
    assistant_id: null,
    group_id: null,
    execution_target: null,
    sort_order: 0,
    created_at: '2026-10-27T00:00:00Z',
    updated_at: '2026-10-27T00:00:00Z',
    last_user_message_at: null,
    messages: [],
    deathmatch_mode: false,
    deathmatch_status: 'inactive',
    deathmatch_goal: null,
    deathmatch_turns: 0,
    deathmatch_max_turns: 0,
    deathmatch_grilling_total: 0,
    deathmatch_grilling_completed: 0,
    deathmatch_grilling_round: 0,
    deathmatch_grilling_round_total: 3,
    deathmatch_grilling_complete: false,
    deathmatch_plan: null,
    deathmatch_plan_version: 0,
    deathmatch_reason: null,
    ...overrides,
  };
}

async function mockConversation(page: Page, id: string, state: Record<string, any>) {
  await page.route(`**/api/conversations/${id}**`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state) });
  });
}

async function send(page: Page, text: string) {
  const input = page.locator('.chat-input[contenteditable]');
  await expect(input).toBeVisible();
  await input.fill(text);
  await page.locator('button.send-btn').click();
}

async function toggleDeathmatch(page: Page, target: 'on' | 'off' = 'off') {
  if (target === 'off') await expect(page.locator('.deathmatch-status-bar')).toHaveCount(1);
  await page.locator('.more-btn').first().click();
  await page.locator('.more-dropdown-item:has-text("死磕模式")').first().click();
  // The click closes the menu. Reopen it and use a fresh locator to verify
  // the actual switch state instead of referencing the unmounted element.
  await page.locator('.more-btn').first().click();
  const item = page.locator('.more-dropdown-item:has-text("死磕模式")').first();
  await expect(item).toBeVisible();
  if (target === 'on') {
    await expect(item).toHaveClass(/active/);
  } else {
    await expect(item).not.toHaveClass(/active/);
  }
  await page.locator('.more-btn').first().click();
  if (target === 'off') await expect(page.locator('.deathmatch-status-bar')).toHaveCount(0);
}

function pendingIds(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const winners = new Map<string, { s: 0 | 1; t: number; r: string }>();
    const offer = (id: string, record: { s: 0 | 1; t: number; r: string } | null) => {
      if (!record) return;
      const old = winners.get(id);
      if (!old || record.t > old.t || (record.t === old.t && record.r > old.r)) winners.set(id, record);
    };
    const read = (storage: Storage, prefix: string) => {
      for (let i = 0; i < storage.length; i++) {
        const key = storage.key(i) || '';
        if (!key.startsWith(prefix) || key.length <= prefix.length) continue;
        const raw = storage.getItem(key);
        if (raw === '1') offer(key.slice(prefix.length), { s: 1, t: 0, r: '' });
        else if (raw === '0') offer(key.slice(prefix.length), { s: 0, t: 0, r: '' });
        else {
          try { offer(key.slice(prefix.length), JSON.parse(raw || 'null')); } catch {}
        }
      }
    };
    read(localStorage, 'chatllm_deathmatch_pending_stops_v1:');
    read(sessionStorage, 'chatllm_deathmatch_pending_stops_v1:session:');
    return [...winners].filter(([, record]) => record.s === 1).map(([id]) => id);
  });
}

async function chatStore(page: Page): Promise<JSHandle<any>> {
  return page.evaluateHandle(() => {
    const app = (document.querySelector('#app') as any).__vue_app__;
    const provides = app?._context?.provides || {};
    const pinia = Object.getOwnPropertySymbols(provides)
      .map(symbol => (provides as any)[symbol])
      .find(value => value?._s);
    return [...(pinia?._s?.values() || [])]
      .find((store: any) => typeof store.setDeathmatchModeEnabled === 'function');
  });
}

test.describe('deathmatch feedback scope/resilience — 8158 production build', () => {
  test.describe.configure({ mode: 'serial' });
  test.afterEach(async ({ request }) => cleanupCreated(request));

  test('new conversation never inherits an armed deathmatch mode/action', async ({ page, request }) => {
    const title = `DM-SCOPE-new-${Date.now()}`;
    const first = await createConversation(request, title);
    const payloads: any[] = [];
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'data: [DONE]\n\n' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await send(page, '先建立一条普通消息，避免空会话复用');
    await toggleDeathmatch(page, 'on');
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(1);
    await page.locator('.new-chat-btn').click();
    await expect(page).not.toHaveURL(new RegExp(first));
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(0);
    await send(page, '新会话应只是普通任务');
    expect(payloads.length).toBeGreaterThanOrEqual(2);
    expect(payloads.at(-1).deathmatch_mode).toBe(false);
    expect(payloads.at(-1).deathmatch_action).toBeNull();
  });

  test('explicit ON during deep-link load survives a stale select snapshot', async ({ page, request }) => {
    const first = await createConversation(request, `DM-SCOPE-select-race-${Date.now()}`);
    const payloads: any[] = [];
    let release!: () => void;
    let seen!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const requestSeen = new Promise<void>((resolve) => { seen = resolve; });
    let intercepted = false;
    await page.route(`**/api/conversations/${first}**`, async (route) => {
      if (!intercepted && route.request().method() === 'GET') {
        intercepted = true;
        seen();
        await gate;
      }
      await route.continue();
    });
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'data: [DONE]\n\n' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`, { waitUntil: 'domcontentloaded' });
    await requestSeen;
    await toggleDeathmatch(page, 'on');
    release();
    await send(page, '深链加载期间的显式 ON 不得被旧快照清除');
    expect(payloads).toHaveLength(1);
    expect(payloads[0].deathmatch_mode).toBe(true);
    expect(payloads[0].deathmatch_action).toBe('start');
  });

  test('ON in A never leaks into ordinary B during selection', async ({ page, request }) => {
    const titleA = `DM-SCOPE-onA-${Date.now()}`;
    const titleB = `DM-SCOPE-ordinaryB-${Date.now()}`;
    const a = await createConversation(request, titleA);
    const b = await createConversation(request, titleB);
    await mockConversation(page, a, conversationState(a, titleA));
    await mockConversation(page, b, conversationState(b, titleB));
    let payload: any = null;
    await page.route('**/api/chat/stream', async (route) => {
      payload = route.request().postDataJSON();
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'data: [DONE]\n\n' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${a}`);
    await toggleDeathmatch(page, 'on');
    await page.getByText(titleB, { exact: true }).first().click();
    await expect(page).toHaveURL(new RegExp(b));
    await send(page, '普通 B 不得继承 A 尚未发送的 ON');
    expect(payload.deathmatch_mode).toBe(false);
    expect(payload.deathmatch_action).toBeNull();
  });

  test('late SSE verdict cannot undo OFF or leak into the next conversation', async ({ page, request }) => {
    const first = await createConversation(request, `DM-SCOPE-late-${Date.now()}`);
    const payloads: any[] = [];
    let release!: () => void;
    let seen!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const requestSeen = new Promise<void>((resolve) => { seen = resolve; });
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      if (payloads.length === 1) {
        seen();
        await gate;
        const verdict = { status: 'active', turns: 1, max_turns: 0, verdict: 'continue', plan_steps: [] };
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\ndata: [DONE]\n\n` });
      } else {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'data: [DONE]\n\n' });
      }
    });
    await page.route(`**/api/chat/stream/stop/${first}`, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"stopped"}' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await toggleDeathmatch(page, 'on');
    await send(page, '开启一个随后才返回 active 的死磕流');
    await requestSeen;
    await toggleDeathmatch(page);
    release();
    await page.waitForTimeout(700);
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(0);
    await page.locator('.new-chat-btn').click();
    await expect(page).not.toHaveURL(new RegExp(first));
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(0);
    await send(page, '关闭后新会话只应普通执行');
    expect(payloads.at(-1).deathmatch_mode).toBe(false);
    expect(payloads.at(-1).deathmatch_action).toBeNull();
  });

  test('OFF survives select-away/select-back and is sent as stop on the next ordinary prompt', async ({ page, request }) => {
    const title = `DM-SCOPE-switch-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true,
      deathmatch_status: 'paused',
      deathmatch_goal: '旧目标',
      deathmatch_grilling_complete: true,
      deathmatch_reason: '旧任务暂停',
    });
    await mockConversation(page, first, state);
    const payloads: any[] = [];
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'data: [DONE]\n\n' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await send(page, '旧会话普通消息，确保新会话不会复用空会话');
    await toggleDeathmatch(page);
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(0);
    await page.locator('.new-chat-btn').click();
    await expect(page).not.toHaveURL(new RegExp(first));
    await page.getByText(title, { exact: true }).first().click();
    await expect(page).toHaveURL(new RegExp(first));
    await send(page, '关闭后同会话的新任务');
    expect(payloads.at(-1).deathmatch_mode).toBe(false);
    expect(payloads.at(-1).deathmatch_action).toBe('stop');
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(0);
  });

  test('grilling answer HTTP 500 rolls back optimistic answer and shows a retryable error', async ({ page, request }) => {
    const first = await createConversation(request, `DM-SCOPE-answer500-${Date.now()}`);
    await mockConversation(page, first, conversationState(first, `DM-SCOPE-answer500-${Date.now()}`, {
      deathmatch_mode: true,
      deathmatch_status: 'grilling',
      deathmatch_grilling_total: 1,
      deathmatch_grilling_round: 1,
    }));
    await page.route('**/api/chat/stream', async (route) => {
      const verdict = {
        status: 'grilling', turns: 0, max_turns: 0, grilling_round: 1, grilling_round_total: 3,
        grilling_completed: 0, grilling_total: 1,
        grilling_questions: [{ task_id: 'dm-answer-500', question_id: 'q1', question: '确认测试约束？', recommendation: '推荐确认', options: ['确认'], round: 1 }],
      };
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\ndata: [DONE]\n\n` });
    });
    await page.route('**/api/agent-tasks/grilling/dm-answer-500/answer', async (route) => {
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"simulated answer failure"}' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await send(page, '渲染一个盘问问题');
    const bubble = page.locator('.grilling-bubble').first();
    await expect(bubble).toBeVisible();
    await bubble.locator('.grilling-option-other').click();
    await bubble.locator('.grilling-answer-input').fill('这个回答应当可以重试');
    await bubble.locator('.grilling-answer-submit-btn').click();
    await expect(page.locator('body')).toContainText('盘问回答提交失败', { timeout: 7000 });
    await expect(bubble.locator('.grilling-q-status')).toHaveCount(0);
    await expect(bubble.locator('.grilling-answer-input')).toHaveValue('这个回答应当可以重试');
    await expect(bubble.locator('.grilling-answer-submit-btn')).toBeEnabled();
  });

  test('paused polling preserves the persisted reason, refreshes messages, and hides continue', async ({ page, request }) => {
    const title = `DM-SCOPE-paused-${Date.now()}`;
    const first = await createConversation(request, title);
    const reason = '连续 5 次评判解析失败（测试持久原因）';
    const state = conversationState(first, title, {
      deathmatch_mode: true,
      deathmatch_status: 'active',
      deathmatch_goal: '验证 paused 呈现',
      deathmatch_grilling_complete: true,
      deathmatch_turns: 1,
    });
    await mockConversation(page, first, state);
    await page.route('**/api/chat/stream', async (route) => {
      const verdict = { status: 'active', turns: 1, max_turns: 0, verdict: 'continue', plan_steps: [] };
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\ndata: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await send(page, '先进入 active，再让轮询发现 paused');
    await expect(page.locator('.deathmatch-status-bar')).toContainText('第 1 轮');
    await page.route(/\/api\/conversations(?:\?.*)?$/, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([state]) });
    });
    state.deathmatch_status = 'paused';
    state.deathmatch_reason = reason;
    state.messages = [{ id: 'paused-evidence-message', conversation_id: first, role: 'assistant', content: `执行已暂停：${reason}`, created_at: '2026-10-27T01:00:00Z' }];
    await expect(page.locator('.dm-status-message')).toContainText(reason, { timeout: 12000 });
    await expect(page.locator('body')).toContainText(`执行已暂停：${reason}`);
    await expect(page.locator('.dm-continue-indicator')).toHaveCount(0);
  });

  test('stop HTTP 500 and reload keep OFF until a real 2xx acknowledgement', async ({ page, request }) => {
    const title = `DM-SCOPE-stop500-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
    });
    await mockConversation(page, first, state);
    const payloads: any[] = [];
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      if (payloads.length === 1) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"stop transport failed"}' });
      } else {
        state.deathmatch_mode = false;
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
      }
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await toggleDeathmatch(page);
    await send(page, '第一次 stop 传输失败，必须保留关闭意图');
    await expect(page.locator('.error-message')).toContainText('500');
    await page.reload();
    await send(page, '第二次 stop 被接受后才能结算');
    expect(payloads.at(-1).deathmatch_mode).toBe(false);
    expect(payloads.at(-1).deathmatch_action).toBe('stop');
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(0);
    await expect.poll(() => pendingIds(page)).toEqual([]);
  });

  test('explicit ON on the empty-chat view survives automatic first conversation creation', async ({ page, request }) => {
    const first = await createConversation(request, `DM-SCOPE-first-on-${Date.now()}`);
    await mockConversation(page, first, conversationState(first, `DM-SCOPE-first-on-${Date.now()}`));
    let payload: any = null;
    await page.route('**/api/chat/stream', async (route) => {
      payload = route.request().postDataJSON();
      const verdict = { status: 'grilling', turns: 0, max_turns: 0, grilling_round: 1, grilling_round_total: 3, grilling_questions: [] };
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\ndata: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await login(page);
    await page.locator('.new-chat-btn').click();
    await expect(page).not.toHaveURL(new RegExp(first));
    await toggleDeathmatch(page, 'on');
    await send(page, '空首页显式开启后的第一条消息');
    await expect.poll(() => payload).not.toBeNull();
    expect(payload.deathmatch_mode).toBe(true);
    expect(payload.deathmatch_action).toBe('start');
    await expect(page.locator('.deathmatch-status-bar')).toContainText('盘问阶段');
  });

  test('grilling completion after OFF confirms the answer but never starts a goal loop', async ({ page, request }) => {
    const first = await createConversation(request, `DM-SCOPE-off-answer-${Date.now()}`);
    await mockConversation(page, first, conversationState(first, `DM-SCOPE-off-answer-${Date.now()}`, {
      deathmatch_mode: true, deathmatch_status: 'grilling', deathmatch_grilling_total: 1, deathmatch_grilling_round: 1,
    }));
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let streamCalls = 0;
    await page.route('**/api/chat/stream', async (route) => {
      streamCalls += 1;
      const verdict = {
        status: 'grilling', turns: 0, max_turns: 0, grilling_round: 1, grilling_round_total: 3,
        grilling_completed: 0, grilling_total: 1,
        grilling_questions: [{ task_id: 'dm-off-answer', question_id: 'q1', question: '确认后是否执行？', recommendation: '推荐确认', options: ['确认'], round: 1 }],
      };
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\ndata: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await page.route('**/api/agent-tasks/grilling/dm-off-answer/answer', async (route) => {
      await gate;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ result: { status: 'grilling_complete', goal: '不能在 OFF 后启动的目标' }, deathmatch_status: 'active', grilling_completed: 1, grilling_total: 1, grilling_round: 1, grilling_round_total: 3 }),
      });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await send(page, '盘问答案在途中时关闭');
    const bubble = page.locator('.grilling-bubble').first();
    await expect(bubble).toBeVisible();
    await bubble.locator('.grilling-option-other').click();
    await bubble.locator('.grilling-answer-input').fill('确认答案');
    await bubble.locator('.grilling-answer-submit-btn').click();
    await toggleDeathmatch(page);
    release();
    await page.waitForTimeout(800);
    expect(streamCalls).toBe(1);
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(0);
  });

  test('authoritative parked mode=false hides the bar and does not resume on an ordinary prompt', async ({ page, request }) => {
    const title = `DM-SCOPE-parked-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'active', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true, deathmatch_turns: 1,
    });
    await mockConversation(page, first, state);
    await page.route('**/api/chat/stream', async (route) => {
      const verdict = { status: 'active', turns: 1, max_turns: 0, verdict: 'continue', plan_steps: [] };
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\ndata: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    const payloads: any[] = [];
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await send(page, '进入 active 后由另一客户端停泊');
    await expect(page.locator('.deathmatch-status-bar')).toContainText('第 1 轮');
    await page.route(/\/api\/conversations(?:\?.*)?$/, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([state]) });
    });
    state.deathmatch_mode = false;
    state.deathmatch_status = 'paused';
    state.deathmatch_reason = '死磕模式已关闭（停泊）';
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(0, { timeout: 12000 });
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await send(page, '停泊后的普通任务');
    expect(payloads.at(-1).deathmatch_mode).toBe(false);
    expect(payloads.at(-1).deathmatch_action).toBeNull();
  });

  test('busy retry clears pending stop only after the retry is acknowledged', async ({ page, request }) => {
    const title = `DM-SCOPE-stop-retry-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
    });
    await mockConversation(page, first, state);
    const payloads: any[] = [];
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      if (payloads.length === 1) {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ error: true, code: 'conversation_busy', conversation_id: first })}\n\ndata: [DONE]\n\n` });
      } else {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
      }
    });
    await page.route(`**/api/chat/stream/stop/${first}`, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"stopped"}' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await toggleDeathmatch(page);
    await send(page, 'busy 后重试的 stop');
    await expect.poll(() => payloads.length).toBe(2);
    expect(payloads.at(-1).deathmatch_action).toBe('stop');
    await expect.poll(() => pendingIds(page)).toEqual([]);
  });

  test('busy then HTTP 500 retry does not acknowledge stop until a later 2xx', async ({ page, request }) => {
    const title = `DM-SCOPE-busy500-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
    });
    await mockConversation(page, first, state);
    const payloads: any[] = [];
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      if (payloads.length === 1) {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ error: true, code: 'conversation_busy', conversation_id: first })}\n\ndata: [DONE]\n\n` });
      } else if (payloads.length === 2) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"retry transport failed"}' });
      } else {
        state.deathmatch_mode = false;
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
      }
    });
    await page.route(`**/api/chat/stream/stop/${first}`, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"stopped"}' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await toggleDeathmatch(page);
    await send(page, 'busy 后 retry 又失败的 stop');
    await expect(page.locator('.error-message')).toContainText('500');
    await send(page, '第三次才真正被接受的 stop');
    await expect.poll(() => payloads.length).toBe(3);
    expect(payloads.at(-1).deathmatch_action).toBe('stop');
    await expect.poll(() => pendingIds(page)).toEqual([]);
  });

  test('OFF A then switch B does not overwrite B mode/action or clear B grilling', async ({ page, request }) => {
    const titleA = `DM-SCOPE-offA-${Date.now()}`;
    const titleB = `DM-SCOPE-currentB-${Date.now()}`;
    const a = await createConversation(request, titleA);
    const b = await createConversation(request, titleB);
    await mockConversation(page, a, conversationState(a, titleA, {
      deathmatch_mode: true, deathmatch_status: 'grilling', deathmatch_grilling_total: 1, deathmatch_grilling_round: 1,
    }));
    await mockConversation(page, b, conversationState(b, titleB));
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    await page.route('**/api/chat/stream', async (route) => {
      const verdict = {
        status: 'grilling', turns: 0, max_turns: 0, grilling_round: 1, grilling_round_total: 3,
        grilling_completed: 0, grilling_total: 1,
        grilling_questions: [{ task_id: 'dm-off-a', question_id: 'q1', question: 'A 的问题？', recommendation: '推荐确认', options: ['确认'], round: 1 }],
      };
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\ndata: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await page.route('**/api/agent-tasks/grilling/dm-off-a/answer', async (route) => {
      await gate;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ result: { status: 'grilling_complete', goal: 'A 的目标不得启动' }, deathmatch_status: 'active', grilling_completed: 1, grilling_total: 1, grilling_round: 1, grilling_round_total: 3 }),
      });
    });
    const payloads: any[] = [];
    await login(page);
    await page.goto(`${APP}/?conv=${a}`);
    await send(page, 'A 的盘问回答正在途中');
    const bubble = page.locator('.grilling-bubble').first();
    await expect(bubble).toBeVisible();
    await bubble.locator('.grilling-option-other').click();
    await bubble.locator('.grilling-answer-input').fill('A 的回答');
    await bubble.locator('.grilling-answer-submit-btn').click();
    await toggleDeathmatch(page);
    await page.getByText(titleB, { exact: true }).first().click();
    await expect(page).toHaveURL(new RegExp(b));
    await toggleDeathmatch(page, 'on');
    release();
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      const verdict = { status: 'grilling', turns: 0, max_turns: 0, grilling_round: 1, grilling_round_total: 3, grilling_questions: [] };
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\ndata: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await send(page, 'B 的独立死磕任务');
    expect(payloads.at(-1).deathmatch_mode).toBe(true);
    expect(payloads.at(-1).deathmatch_action).toBe('start');
    await expect(page.locator('.deathmatch-status-bar')).toContainText('盘问阶段');
  });

  test('deep-link ON clears pending OFF before current id is assigned', async ({ page, request }) => {
    const title = `DM-SCOPE-null-on-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
    });
    await mockConversation(page, first, state);
    await login(page);
    await page.evaluate((id) => {
      const record = JSON.stringify({ s: 1, t: Date.now(), r: 'seed-off' });
      localStorage.setItem(`chatllm_deathmatch_pending_stops_v1:${id}`, record);
      window.history.replaceState({}, '', `/app/frontend/?conv=${id}`);
    }, first);
    const store = await chatStore(page);
    await store.evaluate((s) => {
      s.currentConversationId = null;
      return s.setDeathmatchModeEnabled(true);
    });
    expect(await pendingIds(page)).toEqual([]);
    await store.evaluate((s, id) => s.selectConversation(id), first);
    expect(await store.evaluate(s => ({ mode: s.deathmatchMode, action: s.deathmatchAction })))
      .toEqual({ mode: true, action: 'start' });
  });

  test('deep-link OFF records pending stop before current id is assigned', async ({ page, request }) => {
    const title = `DM-SCOPE-null-off-${Date.now()}`;
    const first = await createConversation(request, title);
    await mockConversation(page, first, conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'active', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
    }));
    await login(page);
    await page.evaluate((id) => {
      window.history.replaceState({}, '', `/app/frontend/?conv=${id}`);
    }, first);
    const store = await chatStore(page);
    await store.evaluate((s) => {
      s.currentConversationId = null;
      return s.setDeathmatchModeEnabled(false);
    });
    expect(await pendingIds(page)).toContain(first);
    expect(await store.evaluate(s => ({ mode: s.deathmatchMode, action: s.deathmatchAction })))
      .toEqual({ mode: false, action: 'stop' });
  });

  test('selection boundary clears the previous conversation mode before GET completes', async ({ page, request }) => {
    const titleA = `DM-SCOPE-select-a-${Date.now()}`;
    const titleB = `DM-SCOPE-select-b-${Date.now()}`;
    const a = await createConversation(request, titleA);
    const b = await createConversation(request, titleB);
    await mockConversation(page, a, conversationState(a, titleA, {
      deathmatch_mode: true, deathmatch_status: 'active', deathmatch_goal: 'A', deathmatch_grilling_complete: true,
    }));
    let release!: () => void;
    let seen!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const requestSeen = new Promise<void>((resolve) => { seen = resolve; });
    await page.route(`**/api/conversations/${b}**`, async (route) => {
      seen();
      await gate;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(conversationState(b, titleB)),
      });
    });
    await login(page);
    const store = await chatStore(page);
    await store.evaluate((s, id) => s.selectConversation(id), a);
    await page.evaluate((id) => {
      window.history.replaceState({}, '', `/app/frontend/?conv=${id}`);
    }, a);
    await store.evaluate(s => s.setDeathmatchModeEnabled(true));
    await page.evaluate((id) => {
      window.history.replaceState({}, '', `/app/frontend/?conv=${id}`);
    }, b);
    const selecting = store.evaluate((s, id) => s.selectConversation(id), b);
    await requestSeen;
    expect(await store.evaluate(s => ({ mode: s.deathmatchMode, action: s.deathmatchAction })))
      .toEqual({ mode: false, action: null });
    release();
    await selecting;
    expect(await store.evaluate(s => ({ mode: s.deathmatchMode, action: s.deathmatchAction })))
      .toEqual({ mode: false, action: null });
  });

  test('late terminal verdict from an old run cannot erase a newer re-ON', async ({ page, request }) => {
    const first = await createConversation(request, `DM-SCOPE-reon-${Date.now()}`);
    const payloads: any[] = [];
    let release!: () => void;
    let seen!: () => void;
    let releaseStop!: () => void;
    let stopSeen!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const requestSeen = new Promise<void>((resolve) => { seen = resolve; });
    const stopGate = new Promise<void>((resolve) => { releaseStop = resolve; });
    const stopRequestSeen = new Promise<void>((resolve) => { stopSeen = resolve; });
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      if (payloads.length === 1) {
        seen();
        await gate;
        const verdict = { status: 'done', turns: 1, max_turns: 0, verdict: 'done', plan_steps: [] };
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\ndata: [DONE]\n\n` });
      } else {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'data: [DONE]\n\n' });
      }
    });
    await page.route(`**/api/chat/stream/stop/${first}`, async (route) => {
      stopSeen();
      await stopGate;
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"stopped"}' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await toggleDeathmatch(page, 'on');
    await send(page, '旧 run 随后才返回 done');
    await requestSeen;
    const store = await chatStore(page);
    const stopping = store.evaluate(s => s.setDeathmatchModeEnabled(false));
    await stopRequestSeen;
    await store.evaluate(s => s.setDeathmatchModeEnabled(true));
    release();
    await page.waitForTimeout(700);
    releaseStop();
    await stopping;
    await page.waitForTimeout(700);
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(1);
    await send(page, '重新 ON 后新 run 不应被旧 done 清除');
    expect(payloads.at(-1).deathmatch_mode).toBe(true);
    expect(payloads.at(-1).deathmatch_action).toBe('start');
  });

  test('create completion never steals an explicit ON owned by another conversation', async ({ page, request }) => {
    const origin = await createConversation(request, `DM-SCOPE-create-origin-${Date.now()}`);
    const created = `dm-create-${Date.now()}`;
    let release!: () => void;
    let seen!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const requestSeen = new Promise<void>((resolve) => { seen = resolve; });
    await page.route('**/api/conversations', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      seen();
      await gate;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(conversationState(created, '新对话')),
      });
    });
    await login(page);
    const store = await chatStore(page);
    await store.evaluate((s, id) => {
      s.currentConversationId = id;
      window.history.replaceState({}, '', '/app/frontend/');
    }, origin);
    const creating = store.evaluate(s => s.createConversation('新对话'));
    await requestSeen;
    await store.evaluate(s => s.setDeathmatchModeEnabled(true));
    release();
    await creating;
    expect(await store.evaluate(s => ({ mode: s.deathmatchMode, action: s.deathmatchAction })))
      .toEqual({ mode: false, action: null });
  });

  test('polling never erases a fresh explicit ON for the current conversation', async ({ page, request }) => {
    const first = await createConversation(request, `DM-SCOPE-poll-intent-${Date.now()}`);
    await login(page);
    const store = await chatStore(page);
    await store.evaluate((s, id) => s.selectConversation(id), first);
    await page.evaluate((id) => {
      window.history.replaceState({}, '', `/app/frontend/?conv=${id}`);
    }, first);
    await store.evaluate(s => s.setDeathmatchModeEnabled(true));
    await store.evaluate((s, id) => {
      s.currentConversationId = id;
      const row = s.conversations.find((item: any) => item.id === id);
      if (row) {
        row.deathmatch_mode = false;
        row.deathmatch_status = 'inactive';
      }
      s.streamStates[id] = {
        ...(s.streamStates[id] || {}),
        streaming: false,
        _lastEventTime: 0,
        deathmatchVerdict: null,
      };
    }, first);
    await store.evaluate(s => s.pollDeathmatchStatus());
    expect(await store.evaluate(s => ({ mode: s.deathmatchMode, action: s.deathmatchAction })))
      .toEqual({ mode: true, action: 'start' });
    await store.evaluate(s => s.stopDeathmatchPolling());
  });

  test('create completion rechecks empty-view ownership after an ON for B', async ({ page, request }) => {
    const b = await createConversation(request, `DM-SCOPE-create-b-${Date.now()}`);
    const created = `dm-create-empty-${Date.now()}`;
    let release!: () => void;
    let seen!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const requestSeen = new Promise<void>((resolve) => { seen = resolve; });
    await page.route('**/api/conversations', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      seen();
      await gate;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(conversationState(created, '新对话')),
      });
    });
    await login(page);
    const store = await chatStore(page);
    await store.evaluate((s) => {
      s.currentConversationId = null;
      window.history.replaceState({}, '', '/app/frontend/');
      return s.setDeathmatchModeEnabled(true);
    });
    const creating = store.evaluate(s => s.createConversation('新对话'));
    await requestSeen;
    await store.evaluate((s, id) => {
      s.currentConversationId = id;
      window.history.replaceState({}, '', `/app/frontend/?conv=${id}`);
      return s.setDeathmatchModeEnabled(true);
    }, b);
    release();
    await creating;
    expect(await store.evaluate(s => ({ mode: s.deathmatchMode, action: s.deathmatchAction })))
      .toEqual({ mode: false, action: null });
  });

  test('in-flight poll and terminal response cannot erase a newer explicit ON', async ({ page, request }) => {
    const title = `DM-SCOPE-poll-flight-${Date.now()}`;
    const first = await createConversation(request, title);
    let release!: () => void;
    let seen!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const requestSeen = new Promise<void>((resolve) => { seen = resolve; });
    await login(page);
    const store = await chatStore(page);
    await page.route(`**/api/conversations/${first}**`, async (route) => {
      seen();
      await gate;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(conversationState(first, title, {
          deathmatch_mode: true, deathmatch_status: 'done', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
        })),
      });
    });
    const polling = store.evaluate((s, id) => {
      s.currentConversationId = id;
      window.history.replaceState({}, '', `/app/frontend/?conv=${id}`);
      let row = s.conversations.find((item: any) => item.id === id);
      if (!row) {
        row = { id, title: 'poll-fixture', deathmatch_goal: '旧目标' };
        s.conversations.push(row);
      }
      row.deathmatch_mode = true;
      row.deathmatch_status = 'active';
      s.streamStates[id] = { ...(s.streamStates[id] || {}), streaming: false, _lastEventTime: 0 };
      return s.pollDeathmatchStatus();
    }, first);
    await requestSeen;
    await store.evaluate((s, id) => {
      s.currentConversationId = id;
      return s.setDeathmatchModeEnabled(true);
    }, first);
    release();
    await polling;
    expect(await store.evaluate(s => ({ mode: s.deathmatchMode, action: s.deathmatchAction })))
      .toEqual({ mode: true, action: 'start' });
    expect(await store.evaluate((s, id) => s.streamStates[id]?.deathmatchVerdict, first)).toBeFalsy();
    await store.evaluate(s => s.stopDeathmatchPolling());
  });

  test('polling completes a legitimate run from the same intent generation', async ({ page, request }) => {
    const title = `DM-SCOPE-poll-current-${Date.now()}`;
    const first = await createConversation(request, title);
    let release!: () => void;
    let seen!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const requestSeen = new Promise<void>((resolve) => { seen = resolve; });
    await login(page);
    const store = await chatStore(page);
    await store.evaluate((s, id) => {
      s.currentConversationId = id;
      window.history.replaceState({}, '', `/app/frontend/?conv=${id}`);
      return s.setDeathmatchModeEnabled(true);
    }, first);
    await page.route(`**/api/conversations/${first}**`, async (route) => {
      seen();
      await gate;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(conversationState(first, title, {
          deathmatch_mode: true, deathmatch_status: 'done', deathmatch_goal: '当前目标', deathmatch_grilling_complete: true,
        })),
      });
    });
    const polling = store.evaluate((s, id) => {
      s.currentConversationId = id;
      let row = s.conversations.find((item: any) => item.id === id);
      if (!row) {
        row = { id, title: 'poll-current', deathmatch_goal: '当前目标' };
        s.conversations.push(row);
      }
      row.deathmatch_mode = true;
      row.deathmatch_status = 'active';
      s.streamStates[id] = {
        ...(s.streamStates[id] || {}),
        streaming: false,
        _lastEventTime: 0,
        // This run belongs to the current intent, so its done is legitimate.
        deathmatchIntentAtRun: 1,
      };
      return s.pollDeathmatchStatus();
    }, first);
    await requestSeen;
    release();
    await polling;
    expect(await store.evaluate(s => ({ mode: s.deathmatchMode, action: s.deathmatchAction })))
      .toEqual({ mode: false, action: null });
    expect(await store.evaluate((s, id) => s.streamStates[id]?.deathmatchVerdict?.status, first)).toBe('done');
  });

  test('resume keeps the original run generation when a newer re-ON exists', async ({ page, request }) => {
    const first = await createConversation(request, `DM-SCOPE-resume-generation-${Date.now()}`);
    await page.route('**/api/chat/stream/resume', async (route) => {
      const verdict = { status: 'done', turns: 1, max_turns: 0, verdict: 'done', plan_steps: [] };
      await route.fulfill({
        status: 200, contentType: 'text/event-stream',
        body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\ndata: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n`,
      });
    });
    await login(page);
    const store = await chatStore(page);
    await store.evaluate((s, id) => {
      s.currentConversationId = id;
      window.history.replaceState({}, '', `/app/frontend/?conv=${id}`);
      s.streamStates[id] = {
        ...(s.streamStates[id] || {}),
        streaming: true,
        abortController: null,
        deathmatchIntentAtRun: 0,
      };
      return s.setDeathmatchModeEnabled(false);
    }, first);
    await store.evaluate((s, id) => {
      s.currentConversationId = id;
      window.history.replaceState({}, '', `/app/frontend/?conv=${id}`);
      return s.setDeathmatchModeEnabled(true);
    }, first);
    expect(await store.evaluate((s, id) => {
      s.streamStates[id].streaming = true;
      s.streamStates[id].abortController = null;
      s.streamStates[id].deathmatchIntentAtRun = 0;
      return s.resumeActiveStream(id);
    }, first)).toBe('resumed');
    expect(await store.evaluate(s => ({ mode: s.deathmatchMode, action: s.deathmatchAction })))
      .toEqual({ mode: true, action: 'start' });
  });

  test('busy retry accepts verdicts from its newer same-conversation attempt', async ({ page, request }) => {
    const first = await createConversation(request, `DM-SCOPE-busy-reon-${Date.now()}`);
    const payloads: any[] = [];
    let busySeen!: () => void;
    const busy = new Promise<void>((resolve) => { busySeen = resolve; });
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      if (payloads.length === 1) {
        busySeen();
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ error: true, code: 'conversation_busy', conversation_id: first })}\n\ndata: [DONE]\n\n` });
      } else {
        const verdict = { status: 'active', turns: 1, max_turns: 0, verdict: 'continue', plan_steps: [] };
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\ndata: [DONE]\n\n` });
      }
    });
    await page.route(`**/api/chat/stream/stop/${first}`, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"stopped"}' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await send(page, '首次 busy，重试期间重新 ON');
    await busy;
    const store = await chatStore(page);
    await store.evaluate(s => s.setDeathmatchModeEnabled(true));
    await expect(page.locator('.deathmatch-status-bar')).toContainText('死磕模式 · 第 1 轮');
    expect(payloads.at(-1).deathmatch_mode).toBe(true);
    expect(payloads.at(-1).deathmatch_action).toBe('start');
  });

  test('poll started after re-ON cannot apply an older run terminal verdict', async ({ page, request }) => {
    const title = `DM-SCOPE-poll-unsent-reon-${Date.now()}`;
    const first = await createConversation(request, title);
    await login(page);
    const store = await chatStore(page);
    await store.evaluate((s, id) => {
      s.currentConversationId = id;
      window.history.replaceState({}, '', `/app/frontend/?conv=${id}`);
      s.streamStates[id] = { ...(s.streamStates[id] || {}), deathmatchIntentAtRun: 0 };
      return s.setDeathmatchModeEnabled(false);
    }, first);
    await store.evaluate(s => s.setDeathmatchModeEnabled(true));
    await page.route(`**/api/conversations/${first}**`, async (route) => {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(conversationState(first, title, {
          deathmatch_mode: true, deathmatch_status: 'done', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
        })),
      });
    });
    await store.evaluate((s, id) => {
      s.currentConversationId = id;
      let row = s.conversations.find((item: any) => item.id === id);
      if (!row) {
        row = { id, title: 'poll-unsent-reon', deathmatch_goal: '旧目标' };
        s.conversations.push(row);
      }
      row.deathmatch_mode = true;
      row.deathmatch_status = 'active';
      s.streamStates[id] = {
        ...(s.streamStates[id] || {}),
        streaming: false,
        _lastEventTime: 0,
        deathmatchIntentAtRun: 0,
      };
      return s.pollDeathmatchStatus();
    }, first);
    expect(await store.evaluate(s => ({ mode: s.deathmatchMode, action: s.deathmatchAction })))
      .toEqual({ mode: true, action: 'start' });
    expect(await store.evaluate((s, id) => s.streamStates[id]?.deathmatchVerdict, first)).toBeFalsy();
  });

  test('multi-tab storage events merge OFF and acknowledgement without resurrection', async ({ page, request }) => {
    const title = `DM-SCOPE-multitab-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
    });
    await mockConversation(page, first, state);
    await page.context().route('**/api/chat/stream', async (route) => {
      state.deathmatch_mode = false;
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await toggleDeathmatch(page);
    await expect.poll(() => pendingIds(page)).toContain(first);
    const pageB = await page.context().newPage();
    await pageB.goto(`${APP}/?conv=${first}`);
    await expect.poll(() => pendingIds(pageB)).toContain(first);
    // Wait for the deep-link selection before sending: otherwise this can
    // exercise an unrelated currently-selected conversation instead of the
    // intended shared pending OFF.
    await pageB.getByText(title, { exact: true }).first().click();
    await expect(pageB.locator('.chat-title')).toContainText(title);
    await pageB.locator('.chat-input[contenteditable]').fill('另一标签页接受 stop');
    await pageB.locator('button.send-btn').click();
    await expect.poll(() => pendingIds(page)).toEqual([]);
    await expect.poll(() => pendingIds(pageB)).toEqual([]);
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(0);
  });

  test('remote OFF in B forces the next request in already-open A to stop', async ({ page, request }) => {
    const title = `DM-SCOPE-remote-off-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
    });
    await mockConversation(page, first, state);
    const payloads: any[] = [];
    await page.context().route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      state.deathmatch_mode = false;
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    const pageB = await page.context().newPage();
    await mockConversation(pageB, first, state);
    await pageB.goto(`${APP}/?conv=${first}`);
    await toggleDeathmatch(pageB);
    await expect.poll(() => pendingIds(page)).toContain(first);
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(0);
    await page.locator('.chat-input[contenteditable]').fill('远程 OFF 后 A 的普通消息');
    await page.locator('button.send-btn').click();
    await expect.poll(() => payloads.length).toBe(1);
    expect(payloads.at(-1).deathmatch_mode).toBe(false);
    expect(payloads.at(-1).deathmatch_action).toBe('stop');
    await expect.poll(() => pendingIds(page)).toEqual([]);
    await expect.poll(() => pendingIds(pageB)).toEqual([]);
  });

  test('write-only storage failure preserves volatile OFF across later reads and selection', async ({ page, request }) => {
    const title = `DM-SCOPE-write-failure-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
    });
    await mockConversation(page, first, state);
    const payloads: any[] = [];
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      state.deathmatch_mode = false;
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await page.evaluate(() => {
      const original = Storage.prototype.setItem;
      Object.defineProperty(localStorage, 'setItem', {
        configurable: true,
        value(key, value) {
          if (key.startsWith('chatllm_deathmatch_pending_stops_v1:')) throw new DOMException('simulated quota', 'QuotaExceededError');
          return original.call(this, key, value);
        },
      });
    });
    await toggleDeathmatch(page);
    await page.evaluate((id) => {
      window.dispatchEvent(new StorageEvent('storage', { key: `chatllm_deathmatch_pending_stops_v1:${id}` }));
    }, first);
    // sessionStorage overlay must preserve an unsaved OFF across reload when
    // localStorage writes fail but reads still work.
    await page.reload();
    await page.locator('.new-chat-btn').click();
    await expect(page).not.toHaveURL(new RegExp(first));
    await page.getByText(title, { exact: true }).first().click();
    await expect(page).toHaveURL(new RegExp(first));
    await send(page, '存储只写失败后仍必须关闭');
    await expect.poll(() => payloads.length).toBe(1);
    expect(payloads.at(-1).deathmatch_mode).toBe(false);
    expect(payloads.at(-1).deathmatch_action).toBe('stop');
  });

  test('remote OFF during busy window aborts retry and the next send stops', async ({ page, request }) => {
    const title = `DM-SCOPE-retry-remote-off-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
    });
    await mockConversation(page, first, state);
    const payloads: any[] = [];
    await page.context().route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      if (payloads.length === 1) {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ error: true, code: 'conversation_busy', conversation_id: first })}\n\ndata: [DONE]\n\n` });
      } else {
        state.deathmatch_mode = false;
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
      }
    });
    let stopSeen!: () => void;
    let releaseStop!: () => void;
    const stopRequest = new Promise<void>((resolve) => { stopSeen = resolve; });
    const stopGate = new Promise<void>((resolve) => { releaseStop = resolve; });
    await page.route(`**/api/chat/stream/stop/${first}`, async (route) => {
      stopSeen();
      await stopGate;
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"stopped"}' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    const pageB = await page.context().newPage();
    await mockConversation(pageB, first, state);
    await pageB.goto(`${APP}/?conv=${first}`);
    await send(page, 'A 的第一个请求 busy，retry 前由 B 关闭');
    await stopRequest;
    await toggleDeathmatch(pageB);
    await expect.poll(() => pendingIds(page)).toContain(first);
    releaseStop();
    // Remote OFF aborts the in-flight flow, so no stale enabled retry may
    // occur. The following fresh request must carry the pending stop instead.
    await page.waitForTimeout(1200);
    expect(payloads.length).toBe(1);
    expect(payloads[0].deathmatch_mode).toBe(true);
    expect(payloads[0].deathmatch_action).toBeNull();
    await send(page, '远程关闭后的下一条普通消息');
    await expect.poll(() => payloads.length).toBe(2);
    expect(payloads[1].deathmatch_mode).toBe(false);
    expect(payloads[1].deathmatch_action).toBe('stop');
    await expect.poll(() => pendingIds(page)).toEqual([]);
    await expect.poll(() => pendingIds(pageB)).toEqual([]);
  });

  test('remote OFF aborts the other tab active stream instead of only hiding it', async ({ page, request }) => {
    const title = `DM-SCOPE-remote-active-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'active', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true, deathmatch_turns: 1,
    });
    await mockConversation(page, first, state);
    let releaseStream!: () => void;
    const streamGate = new Promise<void>((resolve) => { releaseStream = resolve; });
    await page.route('**/api/chat/stream', async (route) => {
      await streamGate;
      const verdict = { status: 'active', turns: 1, max_turns: 0, verdict: 'continue', plan_steps: [] };
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\n` });
    });
    let stopCalls = 0;
    await page.route(`**/api/chat/stream/stop/${first}`, async (route) => {
      stopCalls += 1;
      releaseStream();
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"stopped"}' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    const pageB = await page.context().newPage();
    await mockConversation(pageB, first, state);
    await pageB.goto(`${APP}/?conv=${first}`);
    await expect(page.locator('.chat-title')).toContainText(title);
    await send(page, 'A 保持在途死磕流，B 远程关闭');
    await expect(pageB.locator('.chat-title')).toContainText(title);
    await toggleDeathmatch(pageB);
    await expect.poll(() => stopCalls).toBe(1);
    await expect(page.locator('.deathmatch-status-bar')).toHaveCount(0);
  });

  test('legacy migration preserves every ID when one per-ID write fails', async ({ page, request }) => {
    const titleA = `DM-SCOPE-legacy-a-${Date.now()}`;
    const titleB = `DM-SCOPE-legacy-b-${Date.now()}`;
    const a = await createConversation(request, titleA);
    const b = await createConversation(request, titleB);
    await page.addInitScript(([legacyA, legacyB]) => {
      localStorage.setItem('chatllm_deathmatch_pending_stops_v1', JSON.stringify([legacyA, legacyB]));
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) {
        if (key === `chatllm_deathmatch_pending_stops_v1:${legacyA}`) throw new DOMException('simulated quota', 'QuotaExceededError');
        return original.call(this, key, value);
      };
    }, [a, b]);
    await mockConversation(page, b, conversationState(b, titleB, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: 'B 的旧目标', deathmatch_grilling_complete: true,
    }));
    const payloads: any[] = [];
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${b}`);
    await expect(page.locator('.chat-title')).toContainText(titleB);
    await send(page, 'legacy 第二个 ID 也不能丢');
    await expect.poll(() => payloads.length).toBe(1);
    expect(payloads.at(-1).deathmatch_mode).toBe(false);
    expect(payloads.at(-1).deathmatch_action).toBe('stop');
  });

  test('failed persistent deletion cannot resurrect an acknowledged stop', async ({ page, request }) => {
    const title = `DM-SCOPE-delete-failure-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
    });
    await mockConversation(page, first, state);
    const payloads: any[] = [];
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      state.deathmatch_mode = false;
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await toggleDeathmatch(page);
    await expect.poll(() => pendingIds(page)).toContain(first);
    await page.evaluate(() => {
      const original = Storage.prototype.removeItem;
      Storage.prototype.removeItem = function (key) {
        if (key.startsWith('chatllm_deathmatch_pending_stops_v1:')) throw new DOMException('simulated deletion failure', 'SecurityError');
        return original.call(this, key);
      };
    });
    await send(page, '第一次 stop 已被服务端接受');
    await expect.poll(() => payloads.length).toBe(1);
    await page.evaluate((id) => {
      window.dispatchEvent(new StorageEvent('storage', { key: `chatllm_deathmatch_pending_stops_v1:${id}` }));
    }, first);
    await send(page, '确认后的普通消息不得再次携带 stop');
    await expect.poll(() => payloads.length).toBe(2);
    expect(payloads[0].deathmatch_action).toBe('stop');
    expect(payloads[1].deathmatch_mode).toBe(false);
    expect(payloads[1].deathmatch_action).toBeNull();
  });

  test('busy retry keeps originating conversation flags after selecting another conversation', async ({ page, request }) => {
    const titleA = `DM-SCOPE-retry-origin-a-${Date.now()}`;
    const titleB = `DM-SCOPE-retry-origin-b-${Date.now()}`;
    const a = await createConversation(request, titleA);
    const b = await createConversation(request, titleB);
    await mockConversation(page, a, conversationState(a, titleA, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: 'A 的旧目标', deathmatch_grilling_complete: true,
    }));
    await mockConversation(page, b, conversationState(b, titleB));
    const payloads: any[] = [];
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      if (payloads.length === 1) {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ error: true, code: 'conversation_busy', conversation_id: a })}\n\ndata: [DONE]\n\n` });
      } else {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
      }
    });
    let stopSeen!: () => void;
    let releaseStop!: () => void;
    const stopRequest = new Promise<void>((resolve) => { stopSeen = resolve; });
    const stopGate = new Promise<void>((resolve) => { releaseStop = resolve; });
    await page.route(`**/api/chat/stream/stop/${a}`, async (route) => {
      stopSeen();
      await stopGate;
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"stopped"}' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${a}`);
    await send(page, 'A busy 后切到 B 并开启 B 的死磕');
    await stopRequest;
    await page.getByText(titleB, { exact: true }).first().click();
    await expect(page).toHaveURL(new RegExp(b));
    await expect(page.locator('.chat-title')).toContainText(titleB);
    await toggleDeathmatch(page, 'on');
    releaseStop();
    await expect.poll(() => payloads.length).toBe(2);
    expect(payloads[1].conversation_id).toBe(a);
    expect(payloads[1].deathmatch_mode).toBe(true);
    expect(payloads[1].deathmatch_action).toBeNull();
  });

  test('remote OFF stops a background stream owned by the tab', async ({ page, request }) => {
    const titleA = `DM-SCOPE-background-a-${Date.now()}`;
    const titleB = `DM-SCOPE-background-b-${Date.now()}`;
    const a = await createConversation(request, titleA);
    const b = await createConversation(request, titleB);
    await mockConversation(page, a, conversationState(a, titleA, {
      deathmatch_mode: true, deathmatch_status: 'active', deathmatch_goal: 'A 的旧目标', deathmatch_grilling_complete: true, deathmatch_turns: 1,
    }));
    await mockConversation(page, b, conversationState(b, titleB));
    let releaseStream!: () => void;
    const streamGate = new Promise<void>((resolve) => { releaseStream = resolve; });
    await page.route('**/api/chat/stream', async (route) => {
      await streamGate;
      const verdict = { status: 'active', turns: 1, max_turns: 0, verdict: 'continue', plan_steps: [] };
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ deathmatch_verdict: verdict })}\n\n` });
    });
    let stopCalls = 0;
    await page.route(`**/api/chat/stream/stop/${a}`, async (route) => {
      stopCalls += 1;
      releaseStream();
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"stopped"}' });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${a}`);
    await expect(page.locator('.chat-title')).toContainText(titleA);
    await send(page, 'A 在后台保持活跃流');
    await page.getByText(titleB, { exact: true }).first().click();
    await expect(page).toHaveURL(new RegExp(b));
    const pageB = await page.context().newPage();
    await mockConversation(pageB, a, conversationState(a, titleA, {
      deathmatch_mode: true, deathmatch_status: 'active', deathmatch_goal: 'A 的旧目标', deathmatch_grilling_complete: true, deathmatch_turns: 1,
    }));
    await pageB.goto(`${APP}/?conv=${a}`);
    await expect(pageB.locator('.chat-title')).toContainText(titleA);
    await toggleDeathmatch(pageB);
    await expect.poll(() => stopCalls).toBe(1);
  });

  test('legacy cleanup failure cannot recreate an acknowledged stop', async ({ page, request }) => {
    const title = `DM-SCOPE-legacy-cleanup-${Date.now()}`;
    const first = await createConversation(request, title);
    await page.addInitScript((id) => {
      localStorage.setItem('chatllm_deathmatch_pending_stops_v1', JSON.stringify([id]));
      const original = Storage.prototype.removeItem;
      Storage.prototype.removeItem = function (key) {
        if (key === 'chatllm_deathmatch_pending_stops_v1') throw new DOMException('simulated legacy cleanup failure', 'SecurityError');
        return original.call(this, key);
      };
    }, first);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
    });
    await mockConversation(page, first, state);
    const payloads: any[] = [];
    await page.route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      // The first stop is accepted; subsequent refreshes must see mode=false.
      if (payloads.length === 1) state.deathmatch_mode = false;
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await send(page, 'legacy stop 已确认');
    await expect.poll(() => payloads.length).toBe(1);
    await send(page, 'legacy 不得复活已确认 stop');
    await expect.poll(() => payloads.length).toBe(2);
    expect(payloads[0].deathmatch_action).toBe('stop');
    expect(payloads[1].deathmatch_mode).toBe(false);
    expect(payloads[1].deathmatch_action).toBeNull();
  });

  test('new remote OFF wins over an older failed-deletion tombstone', async ({ page, request }) => {
    const title = `DM-SCOPE-newer-remote-${Date.now()}`;
    const first = await createConversation(request, title);
    const state = conversationState(first, title, {
      deathmatch_mode: true, deathmatch_status: 'paused', deathmatch_goal: '旧目标', deathmatch_grilling_complete: true,
    });
    await mockConversation(page, first, state);
    const payloads: any[] = [];
    await page.context().route('**/api/chat/stream', async (route) => {
      payloads.push(route.request().postDataJSON());
      state.deathmatch_mode = false;
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify({ done: true })}\n\ndata: [DONE]\n\n` });
    });
    await login(page);
    await page.goto(`${APP}/?conv=${first}`);
    await toggleDeathmatch(page);
    await page.evaluate(() => {
      const original = Storage.prototype.removeItem;
      Storage.prototype.removeItem = function (key) {
        if (key.startsWith('chatllm_deathmatch_pending_stops_v1:')) throw new DOMException('simulated deletion failure', 'SecurityError');
        return original.call(this, key);
      };
    });
    await send(page, '旧 stop 删除失败但已确认');
    await expect.poll(() => payloads.length).toBe(1);
    const pageB = await page.context().newPage();
    await mockConversation(pageB, first, state);
    await pageB.goto(`${APP}/?conv=${first}`);
    await toggleDeathmatch(pageB, 'on');
    await toggleDeathmatch(pageB);
    await send(page, '新一轮远程 OFF 必须优先于旧 tombstone');
    await expect.poll(() => payloads.length).toBe(2);
    expect(payloads[1].deathmatch_mode).toBe(false);
    expect(payloads[1].deathmatch_action).toBe('stop');
  });
});
