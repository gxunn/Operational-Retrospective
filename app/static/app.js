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

const viralShell = document.querySelector('[data-viral-shell]');
if (viralShell) {
  const sourceInput = viralShell.querySelector('[data-viral-source-url]');
  const fetchStatusInput = viralShell.querySelector('[data-viral-field="fetch_status"]');
  const fetchErrorInput = viralShell.querySelector('[data-viral-field="fetch_error"]');
  const fetchStatusView = viralShell.querySelector('[data-viral-fetch-status]');
  const fetchErrorBox = viralShell.querySelector('[data-viral-fetch-error]');
  const fetchButton = viralShell.querySelector('[data-viral-fetch-button]');
  const manualButton = viralShell.querySelector('[data-viral-manual-button]');
  const step2 = viralShell.querySelector('[data-viral-step2]');
  const analyzeForm = viralShell.querySelector('[data-viral-analyze-form]');
  const analyzeButton = viralShell.querySelector('[data-viral-analyze-button]');
  const resetButton = viralShell.querySelector('[data-viral-reset-button]');
  const coverPreview = viralShell.querySelector('[data-viral-cover-preview]');
  const coverImage = viralShell.querySelector('[data-viral-cover-image]');
  const fieldNames = ['source_url', 'platform', 'title', 'cover_url', 'cover_description', 'author_name', 'publish_time', 'duration', 'views', 'likes', 'comments', 'collect_count', 'share_count', 'video_text', 'transcript', 'fetch_status', 'fetch_error'];
  const fields = Object.fromEntries(fieldNames.map((name) => [name, viralShell.querySelector(`[data-viral-field="${name}"]`)].filter(Boolean)));

  const setFetchMessage = (status, message) => {
    if (fetchStatusView) fetchStatusView.value = status || '未抓取';
    if (fetchStatusInput) fetchStatusInput.value = status || '未抓取';
    if (fetchErrorInput) fetchErrorInput.value = message || '';
    if (fetchErrorBox) {
      fetchErrorBox.hidden = !message;
      fetchErrorBox.textContent = message || '';
    }
  };

  const populateFields = (payload, options = {}) => {
    if (!payload) return;
    const replaceEmptyOnly = Boolean(options.replaceEmptyOnly);
    const numericFields = new Set(['views', 'likes', 'comments', 'collect_count', 'share_count']);
    Object.entries(payload).forEach(([name, value]) => {
      const field = fields[name];
      if (!field) return;
      let text = value === null || value === undefined ? '' : String(value);
      if (numericFields.has(name)) {
        text = text.replace(/,/g, '').replace(/，/g, '');
        if (!text || text === '数据未填写') text = '0';
      }
      if (replaceEmptyOnly && field.value && field.value.trim()) return;
      if (text || !replaceEmptyOnly) field.value = text;
    });
    if (coverPreview && coverImage) {
      const coverUrl = String(payload.cover_url || '').trim();
      if (coverUrl) {
        coverImage.src = coverUrl;
        coverPreview.hidden = false;
      } else {
        coverPreview.hidden = true;
      }
    }
    if (step2) step2.hidden = false;
  };

  const setBusy = (busy) => {
    if (fetchButton) fetchButton.disabled = busy;
    if (analyzeButton) analyzeButton.disabled = busy;
    if (resetButton) resetButton.disabled = busy;
  };

  if (sourceInput && fields.source_url) {
    sourceInput.addEventListener('input', () => {
      fields.source_url.value = sourceInput.value;
    });
  }

  if (manualButton && step2) {
    manualButton.addEventListener('click', () => {
      step2.hidden = false;
      flashNotice('已打开手动填写');
      if (sourceInput) sourceInput.focus();
    });
  }

  if (resetButton && analyzeForm && step2) {
    resetButton.addEventListener('click', () => {
      analyzeForm.reset();
      setFetchMessage('未抓取', '');
      if (coverPreview) coverPreview.hidden = true;
      step2.hidden = true;
      if (sourceInput) sourceInput.focus();
    });
  }

  if (fetchButton && sourceInput) {
    fetchButton.addEventListener('click', async () => {
      const videoUrl = sourceInput.value.trim();
      if (!videoUrl) {
        flashNotice('请先输入视频链接');
        sourceInput.focus();
        return;
      }
      setBusy(true);
      setFetchMessage('抓取中', '');
      try {
        const response = await fetch('/api/viral/fetch-video-info', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ video_url: videoUrl }),
        });
        const payload = await response.json();
        const data = payload.data || payload;
        populateFields({ source_url: videoUrl, ...data }, { replaceEmptyOnly: !payload.ok });
        if (payload.ok) {
          setFetchMessage('抓取成功', '');
          flashNotice('抓取成功');
        } else {
          const error = payload.fetch_error || '该平台可能限制自动抓取，请手动补充视频信息后继续拆解。';
          setFetchMessage('抓取失败，可手动填写', error);
          flashNotice(error);
        }
      } catch (_) {
        const error = '该平台可能限制自动抓取，请手动补充视频信息后继续拆解。';
        setFetchMessage('抓取失败，可手动填写', error);
        flashNotice(error);
      } finally {
        setBusy(false);
      }
    });
  }

  if (analyzeForm) {
    analyzeForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const formData = new FormData(analyzeForm);
      const sourceUrl = String(formData.get('source_url') || sourceInput?.value || '').trim();
      if (!sourceUrl) {
        flashNotice('请先输入视频链接');
        return;
      }
      const payload = Object.fromEntries(formData.entries());
      payload.source_url = sourceUrl;
      setBusy(true);
      try {
        const response = await fetch('/api/viral/analyze', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok || result.error) {
          flashNotice(result.error || '拆解失败');
          if (result.id) window.location.href = `/breakdown/${result.id}`;
          return;
        }
        flashNotice(result.message || '拆解任务已开始');
        window.location.href = `/breakdown/${result.id}`;
      } catch (_) {
        flashNotice('拆解失败，请稍后重试');
      } finally {
        setBusy(false);
      }
    });
  }
}

const breakdownDetail = document.querySelector('[data-breakdown-detail]');
if (breakdownDetail) {
  const statusUrl = breakdownDetail.dataset.breakdownStatusUrl;
  const statusPill = breakdownDetail.querySelector('[data-breakdown-status-pill]');
  const progressBar = breakdownDetail.querySelector('[data-breakdown-progress-bar]');
  const progressText = breakdownDetail.querySelector('[data-breakdown-progress-text]');
  const taskTip = breakdownDetail.querySelector('[data-breakdown-task-tip]');
  const errorBox = breakdownDetail.querySelector('[data-breakdown-error]');
  const markdownBox = breakdownDetail.querySelector('[data-breakdown-markdown]');
  let polling = null;

  const renderState = (payload) => {
    if (!payload) return;
    if (statusPill) statusPill.textContent = payload.analysis_status || payload.status || '未开始';
    if (progressBar) progressBar.style.width = `${Math.max(0, Math.min(100, Number(payload.progress || 0)))}%`;
    if (progressText) progressText.textContent = `${Math.max(0, Math.min(100, Number(payload.progress || 0)))}%`;
    if (taskTip) {
      taskTip.textContent = payload.status === '失败'
        ? (payload.error_message || '失败：未生成拆解结果')
        : (payload.status === '分析中' ? '正在分析，页面会自动刷新' : '已保存到案例库');
    }
    if (errorBox) {
      const failed = payload.status === '失败' && payload.error_message;
      errorBox.hidden = !failed;
      errorBox.textContent = failed ? payload.error_message : '';
    }
    if (markdownBox && payload.markdown_html) markdownBox.innerHTML = payload.markdown_html;
  };

  const stopPolling = () => {
    if (polling) window.clearInterval(polling);
    polling = null;
  };

  const fetchState = async () => {
    try {
      const response = await fetch(statusUrl, { headers: { Accept: 'application/json' } });
      if (!response.ok) {
        stopPolling();
        return;
      }
      const payload = await response.json();
      renderState(payload);
      if ((payload.analysis_status || payload.status) !== '分析中') stopPolling();
    } catch (_) {
      stopPolling();
    }
  };

  const initialStatus = breakdownDetail.dataset.breakdownStatus || '未开始';
  if (initialStatus === '分析中') {
    fetchState();
    polling = window.setInterval(fetchState, 2000);
  }
}

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
