const menuButton = document.querySelector('[data-menu]');
const sidebar = document.querySelector('#sidebar');
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
    fileName.textContent = fileInput.files[0]?.name || '尚未选择文件';
  });
}

const toast = document.querySelector('.toast');
if (toast) setTimeout(() => toast.classList.add('leaving'), 4500);

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
