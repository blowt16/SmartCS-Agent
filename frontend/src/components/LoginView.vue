<template>
  <div class="login-container">
    <div class="login-box">
      <h1 class="login-title">SmartCS-Agent</h1>

      <!-- 登录/注册 tab -->
      <div class="flex items-center justify-center gap-6 mb-8">
        <span
          class="login-link text-base"
          :class="mode === 'login' ? 'font-semibold' : ''"
          @click="switchMode('login')"
        >账号登录</span>
        <span class="text-gray-600">|</span>
        <span
          class="login-link text-base"
          :class="mode === 'register' ? 'font-semibold' : ''"
          @click="switchMode('register')"
        >注册账号</span>
      </div>

      <form @submit.prevent="submit">
        <div v-if="mode === 'register'" class="mb-4">
          <input
            v-model="username"
            type="text"
            placeholder="用户名"
            class="login-input"
          >
        </div>

        <!-- 登录表单：状态独立，不随模式切换改变，保真记住的账密 -->
        <div v-if="mode === 'login'" class="mb-4">
          <input
            v-model="email"
            type="email"
            autocomplete="username"
            placeholder="请输入邮箱"
            class="login-input"
          >
        </div>

        <div v-if="mode === 'login'" class="mb-4">
          <input
            v-model="password"
            type="password"
            autocomplete="current-password"
            placeholder="请输入密码"
            class="login-input"
          >
        </div>

        <!-- 注册表单：独立字段，始终空白进入，不受记住功能影响 -->
        <div v-if="mode === 'register'" class="mb-4">
          <input
            v-model="emailReg"
            type="email"
            autocomplete="off"
            placeholder="请输入邮箱"
            class="login-input"
          >
        </div>

        <div v-if="mode === 'register'" class="mb-4">
          <input
            v-model="passwordReg"
            type="password"
            autocomplete="new-password"
            placeholder="请输入密码"
            class="login-input"
          >
        </div>

        <div v-if="mode === 'login'" class="mb-4 flex items-center">
          <input
            id="remember"
            v-model="remember"
            type="checkbox"
            class="mr-2"
            @change="onRememberChange"
          >
          <label for="remember" class="text-sm text-gray-500 cursor-pointer">记住账号密码</label>
        </div>

        <div v-if="mode === 'register'" class="mb-4">
          <input
            v-model="confirmPassword"
            type="password"
            autocomplete="new-password"
            placeholder="请确认密码"
            class="login-input"
          >
        </div>

        <p v-if="error" class="login-error">{{ error }}</p>
        <p v-if="successMsg" class="text-green-400 text-sm mb-3">{{ successMsg }}</p>

        <button type="submit" :disabled="loading" class="login-btn">
          <i v-if="loading" class="fas fa-spinner fa-spin mr-2"></i>
          {{ mode === 'login' ? '登 录' : '注 册' }}
        </button>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue';
import { login as apiLogin, register as apiRegister } from '../api/auth.js';

const emit = defineEmits(['logged-in']);

const mode = ref('login');
const username = ref('');
const email = ref('');
const password = ref('');
const confirmPassword = ref('');
const emailReg = ref('');
const passwordReg = ref('');
const error = ref('');
const successMsg = ref('');
const loading = ref(false);
const remember = ref(false);

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const PASSWORD_RE = /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$/;
const REMEMBER_KEY = 'remembered-credentials';

// 页面加载时恢复记住的账号密码
const savedCred = localStorage.getItem(REMEMBER_KEY);
if (savedCred) {
  try {
    const cred = JSON.parse(savedCred);
    email.value = cred.email || '';
    password.value = cred.password || '';
    remember.value = true;
  } catch {
    localStorage.removeItem(REMEMBER_KEY);
  }
}

function switchMode(m) {
  mode.value = m;
  error.value = '';
  successMsg.value = '';
}

// 取消勾选后立即清除已保存的账号密码
function onRememberChange() {
  if (!remember.value) {
    localStorage.removeItem(REMEMBER_KEY);
  }
}

function validate() {
  const emailVal = mode.value === 'register' ? emailReg.value : email.value;
  const passwordVal = mode.value === 'register' ? passwordReg.value : password.value;
  if (mode.value === 'register' && !username.value.trim()) {
    error.value = '请输入用户名';
    return false;
  }
  if (!emailVal.trim()) {
    error.value = '请输入邮箱';
    return false;
  }
  if (!EMAIL_RE.test(emailVal)) {
    error.value = '请输入有效的邮箱地址';
    return false;
  }
  if (!passwordVal) {
    error.value = '请输入密码';
    return false;
  }
  if (!PASSWORD_RE.test(passwordVal)) {
    error.value = '密码必须包含大小写字母和数字，至少8位';
    return false;
  }
  if (mode.value === 'register' && passwordReg.value !== confirmPassword.value) {
    error.value = '两次输入的密码不一致';
    return false;
  }
  return true;
}

async function submit() {
  error.value = '';
  successMsg.value = '';
  if (!validate()) return;

  loading.value = true;
  try {
    if (mode.value === 'register') {
      await apiRegister(username.value.trim(), emailReg.value, passwordReg.value);
      successMsg.value = '注册成功';
      mode.value = 'login';
      // 注册的邮箱带入登录表单，方便立即登录；注册字段整体清空防残留
      email.value = emailReg.value;
      username.value = '';
      emailReg.value = '';
      passwordReg.value = '';
      confirmPassword.value = '';
    } else {
      await apiLogin(email.value, password.value);
      if (remember.value) {
        localStorage.setItem(REMEMBER_KEY, JSON.stringify({ email: email.value, password: password.value }));
      } else {
        localStorage.removeItem(REMEMBER_KEY);
      }
      emit('logged-in');
    }
  } catch (e) {
    error.value = e.message || '操作失败，请重试';
  } finally {
    loading.value = false;
  }
}
</script>
