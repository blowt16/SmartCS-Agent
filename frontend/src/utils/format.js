// 时间与文件格式化工具（与 chat.html 一致）

export function formatTime(date) {
  const now = new Date();
  const diff = now - new Date(date);
  const minutes = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  const days = Math.floor(diff / 86400000);

  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  if (hours < 24) return `${hours} 小时前`;
  return `${days} 天前`;
}

export function formatMessageTime(date) {
  return new Date(date).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

export function getDocIcon(type) {
  const icons = {
    pdf: 'fas fa-file-pdf text-red-500',
    doc: 'fas fa-file-word text-blue-500',
    docx: 'fas fa-file-word text-blue-500',
    txt: 'fas fa-file-alt text-gray-500',
  };
  return icons[type] || 'fas fa-file text-gray-500';
}

export function getDocIconBg(type) {
  const bgs = {
    pdf: 'bg-red-100',
    doc: 'bg-blue-100',
    docx: 'bg-blue-100',
    txt: 'bg-gray-100',
  };
  return bgs[type] || 'bg-gray-100';
}
