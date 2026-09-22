import { createApp } from 'vue';
import AdminApp from './AdminApp.vue';
import './admin.css';
import '@fontsource/inter/400.css';
import '@fontsource/inter/500.css';
import '@fontsource/inter/600.css';
import '@fontsource/inter/700.css';
import '@fortawesome/fontawesome-free/css/all.min.css';

// 这一份导入清单必须逐行写全,漏一样就少一块(且不报错):
//   漏 ./admin.css          → 整个管理端无样式(Tailwind 指令不落地)
//   漏 fontawesome all.min  → 导航/卡片图标与缩略图兜底图标全部渲染成空白
//   漏 @fontsource/inter/*  → tailwind.config.cjs 的 fontFamily.sans 静默回退 system-ui
// 不需要 highlight.js(管理端无 markdown 渲染,那是聊天消息专用的)。
createApp(AdminApp).mount('#admin-app');
