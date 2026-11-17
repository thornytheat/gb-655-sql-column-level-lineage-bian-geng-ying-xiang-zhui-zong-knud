/* 表单与请求：动作集中在 ACTIONS 里，失败必须写到页面上 */
const ACTIONS = {
  async import() {
    return request('/api/schemas/import', {
      files: ['schema_v2026_06.sql', 'schema_v2026_09.sql'],
    });
  },

  async run(button) {
    const body = {};
    if (button.dataset.forceStale) {
      body.base_version_seq = 0;
    }
    const run = await request('/api/runs', body);
    return run.reused
      ? `命中已有批次 ${run.run_id}`
      : `新批次 ${run.run_id} 序号 ${run.version_seq}`;
  },

  async 'restore-archive'() {
    const input = document.querySelector('[data-archive-file]');
    if (!input || !input.files || !input.files[0]) {
      throw new Error('先选一个归档包');
    }
    const body = await input.files[0].arrayBuffer();
    const result = await request('/api/archives/restore', body, 'application/json');
    if (result.status === 'duplicate') {
      return `同一归档已入过，回读批次 ${result.run_id}`;
    }
    return `已恢复 ${result.run_id}　本地顺序 ${result.local_seq}　来源 ${result.source.source_id}#${result.source.source_version_seq}`;
  },

  async 'replay-imports'() {
    const result = await request('/api/archives/replay', {});
    return `续跑 ${result.results.length} 条`;
  },

  async 'compare-types'() {
    const oldValue = document.getElementById('old-type');
    const newValue = document.getElementById('new-type');
    const result = await request('/api/types/compare', {
      old: oldValue ? oldValue.value : '',
      new: newValue ? newValue.value : '',
    });
    const target = document.querySelector('[data-compare-result]');
    if (target) {
      const detail = result.detail ? `/${result.detail}` : '';
      target.textContent = `${result.old.norm} → ${result.new.norm}　${result.change}${detail}　${result.breaking ? '破坏' : '兼容'}`;
    }
    return `比较完成 ${result.change}`;
  },
};

async function request(url, body, contentType) {
  let payloadBody = body;
  let type = contentType;
  if (!type) {
    type = 'application/json';
    payloadBody = JSON.stringify(body || {});
  }
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': type },
    body: payloadBody,
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

function flash(target, message, bad) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('is-bad', Boolean(bad));
  target.classList.toggle('is-ok', !bad);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const name = button.dataset.action;
  const handler = ACTIONS[name];
  if (!handler) return;
  const target = document.querySelector('[data-flash]');
  button.disabled = true;
  flash(target, '…', false);
  try {
    const message = await handler(button);
    flash(target, message || '完成', false);
    if (name === 'run' || name === 'restore-archive' || name === 'replay-imports') {
      setTimeout(() => window.location.reload(), 400);
    }
  } catch (error) {
    flash(target, error.message, true);
  } finally {
    button.disabled = false;
  }
});
