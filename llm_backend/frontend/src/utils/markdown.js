// Markdown 渲染（marked v4：highlight + breaks，与 chat.html 一致）
import { marked } from 'marked';
import hljs from 'highlight.js';

marked.setOptions({
  highlight: function (code, lang) {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, { language: lang }).value;
    }
    return hljs.highlightAuto(code).value;
  },
  breaks: true,
});

export function renderMarkdown(text) {
  if (!text) return '';
  return marked.parse(text);
}
