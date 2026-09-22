// 索引任务 store —— 模块级单例,【不是】KnowledgeView 的组件状态。
//
// 这是关键设计点:用户在处理中切到商品管理页时 KnowledgeView 会卸载;若状态与请求都挂在
// 组件上,要么状态丢失、要么被 abort。挂在模块作用域则:
//   - 组件卸载不影响它(普通 fetch 的 promise 本来就不随组件生命周期销毁)
//   - 切回来还能看到进度
//   - 完成后由 store 通知列表刷新
// 代价:刷新浏览器仍会中断(HTTP 连接随页面销毁)。服务端侧安全 —— Starlette 取消生成器
// → 取消索引任务;而 process_file 是"最后一步单事务写入",取消在写入前 = 零写入,不留半成品。
import { reactive } from 'vue';
import { authHeaders, handleUnauthorized } from '../api/auth.js';

export const knowledgeJob = reactive({
  active: false,       // 是否正在处理
  percent: 0,          // 0-100
  stage: '',           // 阶段标签:"解析文档" / "生成向量" …
  detail: '',          // 细分说明:"嵌入中 3/8 批" / "MinerU 云端解析中…"
  error: null,         // 失败时的错误文案;非 null 即失败态
  done: false,         // 成功完成(用于列表刷新与淡出)
  doneId: null,        // 完成后写入的文档 id,KnowledgeView watch 它来刷新列表
  fileName: '',        // 本次处理的文件名(成功提示要带名字,见下)
});

// 每次 start 自增;用于丢弃过期流的回调。
// 用户可能在一次失败后马上重试,此时旧流的回调若还在飞,会把新任务的状态覆盖掉。
// 用序号比对一行解决,不用 AbortController 主动断开(旧流让它自然结束)。
let seq = 0;

function fail(msg, expect = seq) {
  if (expect !== seq) return;
  Object.assign(knowledgeJob, {
    active: false, error: msg, percent: 0, stage: '', detail: '',
  });
}

function applyEvent(evt) {
  if (evt.type === 'progress') {
    knowledgeJob.percent = evt.percent;
    knowledgeJob.stage = evt.stage;
    knowledgeJob.detail = evt.detail || '';
  } else if (evt.type === 'done') {
    knowledgeJob.active = false;
    knowledgeJob.done = true;
    knowledgeJob.percent = 100;
    knowledgeJob.detail = `${evt.document.chunk_count} 个片段`;
    knowledgeJob.doneId = evt.document.id;      // ← KnowledgeView watch 这个
  } else if (evt.type === 'error') {
    fail(evt.detail || evt.error || '处理失败');
  }
}

export async function startCommit({ md5, original_filename, description }) {
  const my = ++seq;
  Object.assign(knowledgeJob, {
    active: true, percent: 0, stage: '准备中', detail: '',
    error: null, done: false, doneId: null, fileName: original_filename,
  });

  // 1) 发起请求:这一步可能直接返回 4xx(暂存过期 / md5 非法),那是普通 JSON 错误
  let res;
  try {
    res = await fetch('/api/admin/knowledge/commit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ md5, original_filename, description }),
    });
  } catch {
    return fail('网络错误，请重试', my);
  }
  if (handleUnauthorized(res)) return fail('登录已失效', my);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));   // 流没开始,还是普通 JSON
    return fail(data.detail || `提交失败: ${res.status}`, my);
  }

  // 2) 读 SSE 流:分帧格式与客户端 useChat.js 一致(reader + 按行切 + 取 data: 前缀)
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop();
      for (const line of lines) {
        const t = line.trim();
        if (!t.startsWith('data:')) continue;
        const raw = t.slice(5).trim();
        if (!raw) continue;
        let evt;
        try { evt = JSON.parse(raw); } catch { continue; }
        if (my !== seq) return;                     // 有更新的一次 start,丢弃本次
        applyEvent(evt);
      }
    }
  } catch {
    return fail('连接中断，请重试', my);
  }
}

export function dismissJob() {
  Object.assign(knowledgeJob, {
    active: false, percent: 0, stage: '', detail: '',
    error: null, done: false, doneId: null, fileName: '',
  });
}
