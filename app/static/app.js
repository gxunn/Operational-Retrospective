const menuButton = document.querySelector('[data-menu]');
const sidebar = document.querySelector('#sidebar');

function flashNotice(message) {
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.textContent = message;
  document.body.appendChild(toast);
  window.setTimeout(() => toast.classList.add('leaving'), 3000);
  window.setTimeout(() => toast.remove(), 3400);
}

if (menuButton && sidebar) {
  menuButton.addEventListener('click', () => sidebar.classList.toggle('open'));
  document.addEventListener('click', (event) => {
    if (sidebar.classList.contains('open') && !sidebar.contains(event.target) && !menuButton.contains(event.target)) sidebar.classList.remove('open');
  });
}

const collapseButton = document.querySelector('[data-collapse]');
if (collapseButton) {
  collapseButton.addEventListener('click', () => document.body.classList.toggle('sidebar-collapsed'));
}

const fileInput = document.querySelector('.drop-zone input[type="file"]');
const fileName = document.querySelector('[data-file-name]');
if (fileInput && fileName) {
  fileInput.addEventListener('change', () => {
    fileName.textContent = fileInput.files.length ? Array.from(fileInput.files).map((file) => file.name).join('、') : '尚未选择文件';
  });
}

const multiUploadForm = document.querySelector('[data-multi-upload]');
if (multiUploadForm) {
  multiUploadForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const progress = multiUploadForm.querySelector('[data-upload-progress]');
    const bar = multiUploadForm.querySelector('[data-upload-progress-bar]');
    const result = multiUploadForm.querySelector('[data-upload-result]');
    const files = fileInput?.files;
    if (!files?.length) return;
    const formData = new FormData();
    formData.append('csrf', multiUploadForm.querySelector('input[name="csrf"]').value);
    formData.append('account_id', multiUploadForm.querySelector('select[name="account_id"]').value);
    Array.from(files).forEach((file) => formData.append('files', file));
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/imports/upload-multi');
    progress.hidden = false;
    result.textContent = '';
    xhr.upload.addEventListener('progress', (e) => {
      if (!e.lengthComputable) return;
      bar.style.width = `${Math.round((e.loaded / e.total) * 100)}%`;
    });
    xhr.onload = () => {
      try {
        const payload = JSON.parse(xhr.responseText);
        result.innerHTML = (payload.results || []).map((item) => `<p>${item.name}：${item.status}${item.message ? ` - ${item.message}` : ''}</p>`).join('');
      } catch (_) {
        result.textContent = '上传完成，但返回结果解析失败。';
      }
    };
    xhr.onerror = () => {
      result.innerHTML = '<p>上传失败，请重试。</p><button type="button" class="button secondary" onclick="location.reload()">重试</button>';
    };
    xhr.send(formData);
  });
}

const toast = document.querySelector('.toast');
if (toast) setTimeout(() => toast.classList.add('leaving'), 4500);

document.querySelectorAll('[data-bar-width]').forEach((bar) => {
  const value = Number(bar.dataset.barWidth || 0);
  bar.style.width = `${Math.max(0, Math.min(100, value))}%`;
});

document.querySelectorAll('[data-x][data-y]').forEach((label) => {
  label.style.left = `${label.dataset.x}%`;
  label.style.top = `${label.dataset.y}%`;
});

document.querySelectorAll('[data-copy]').forEach((button) => {
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      const original = button.textContent;
      button.textContent = '已复制';
      setTimeout(() => { button.textContent = original; }, 1400);
    } catch (_) {
      button.textContent = '复制失败';
    }
  });
});

document.querySelectorAll('form[data-confirm], button[data-confirm]').forEach((item) => {
  item.addEventListener('submit', (event) => {
    if (!window.confirm(item.dataset.confirm || '确认继续吗？')) event.preventDefault();
  });
  item.addEventListener('click', (event) => {
    if (item.tagName === 'BUTTON' && item.form && !window.confirm(item.dataset.confirm || '确认继续吗？')) event.preventDefault();
  });
});

const assistantToggle = document.querySelector('[data-assistant-toggle]');
const assistantPanel = document.querySelector('[data-assistant-panel]');
const assistantClose = document.querySelector('[data-assistant-close]');
const assistantForm = document.querySelector('[data-assistant-form]');
const assistantLog = document.querySelector('[data-assistant-log]');
const openaiConfigured = document.body?.dataset.openaiConfigured === 'true';

document.querySelectorAll('[data-ai-required]').forEach((element) => {
  if (openaiConfigured) return;
  if ('disabled' in element) element.disabled = true;
  element.setAttribute('aria-disabled', 'true');
  if (!element.title) element.title = '未配置API密钥';
});

function appendAssistantMessage(role, text) {
  if (!assistantLog) return;
  const item = document.createElement('div');
  item.className = `assistant-msg ${role}`;
  item.textContent = text;
  assistantLog.appendChild(item);
  assistantLog.scrollTop = assistantLog.scrollHeight;
}

if (assistantToggle && assistantPanel) {
  assistantToggle.addEventListener('click', () => {
    if (!openaiConfigured) {
      flashNotice('未配置API密钥');
      return;
    }
    assistantPanel.hidden = false;
    assistantPanel.classList.add('open');
  });
}
if (assistantClose && assistantPanel) {
  assistantClose.addEventListener('click', () => {
    assistantPanel.hidden = true;
    assistantPanel.classList.remove('open');
  });
}
if (assistantForm) {
  assistantForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const formData = new FormData(assistantForm);
    const question = String(formData.get('question') || '').trim();
    if (!question) return;
    appendAssistantMessage('user', question);
    assistantForm.reset();
    appendAssistantMessage('assistant', '正在分析中...');
    try {
      const response = await fetch('/assistant/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      });
      const data = await response.json();
      assistantLog.lastElementChild?.remove();
      appendAssistantMessage('assistant', data.answer || data.error || '暂时没有结果。');
    } catch (_) {
      assistantLog.lastElementChild?.remove();
      appendAssistantMessage('assistant', '请求失败，请稍后再试。');
    }
  });
}
