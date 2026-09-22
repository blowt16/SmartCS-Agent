<template>
  <div class="min-h-screen flex items-center justify-center p-6">
    <div class="bg-white rounded-2xl shadow-sm border border-gray-100 p-8 w-full" style="max-width: 400px">
      <h1 class="text-xl font-bold text-gray-800 text-center">SmartCS-Agent 管理端</h1>
      <p class="text-sm text-gray-400 text-center mt-2 mb-7">请使用管理员账号登录</p>

      <form @submit.prevent="submit">
        <div class="mb-4">
          <input
            v-model="email"
            type="email"
            autocomplete="username"
            placeholder="请输入邮箱"
            class="w-full border border-gray-200 rounded-lg px-4 py-3 text-sm outline-none transition-colors focus:border-primary"
          >
        </div>

        <div class="mb-4">
          <input
            v-model="password"
            type="password"
            autocomplete="current-password"
            placeholder="请输入密码"
            class="w-full border border-gray-200 rounded-lg px-4 py-3 text-sm outline-none transition-colors focus:border-primary"
          >
        </div>

        <p v-if="error" class="text-red-500 text-sm mb-3">{{ error }}</p>

        <button
          type="submit"
          :disabled="loading"
          class="w-full bg-primary hover:bg-primary-dark disabled:opacity-60 disabled:cursor-not-allowed text-white rounded-lg py-3 text-sm font-medium transition-colors"
        >
          <i v-if="loading" class="fas fa-spinner fa-spin mr-2"></i>
          登 录
        </button>
      </form>

      <!-- 必须有,否则误入管理端的用户没有退路 -->
      <div class="text-center mt-5">
        <a href="/" class="text-sm text-primary hover:text-primary-dark">返回客服端</a>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue';
import { login as apiLogin } from '../../api/auth.js';

// 与客户端 LoginView 的差异(见 spec §6.3.1):
//   - 取【浅色】:管理端是独立站点,登录页是它的第一屏,与后面 5 个浅色页面一致更重要
//   - 【不做】「记住账号密码」:客户端那个把密码明文写进 localStorage,管理端不复制这个做法
//   - 【不做】「注册」入口:管理员账号由种子脚本创建
//   - 错误提示【不区分】"密码错"和"非管理员":避免泄露账号是否存在;
//     非管理员由登录后的 role 判断给出(AdminApp 的 denied 态)
const emit = defineEmits(['logged-in']);

const email = ref('');
const password = ref('');
const error = ref('');
const loading = ref(false);

async function submit() {
  error.value = '';
  if (!email.value.trim()) { error.value = '请输入邮箱'; return; }
  if (!password.value) { error.value = '请输入密码'; return; }

  loading.value = true;
  try {
    // login() 内部会 setToken
    await apiLogin(email.value, password.value);
    emit('logged-in');
  } catch (e) {
    error.value = e.message || '登录失败，请重试';
  } finally {
    loading.value = false;
  }
}
</script>
