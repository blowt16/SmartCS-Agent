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

        <div class="mb-4">
          <input
            v-model="email"
            type="email"
            placeholder="请输入邮箱"
            class="login-input"
          >
        </div>

        <div class="mb-4">
          <input
            v-model="password"
            type="password"
            placeholder="请输入密码"
            class="login-input"
          >
        </div>

        <div v-if="mode === 'register'" class="mb-4">
          <input
            v-model="confirmPassword"
            type="password"
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

      <p class="text-center mt-6 text-sm">
        <template v-if="mode === 'login'">
          还没有账号？
          <span class="login-link" @click="switchMode('register')">立即注册</span>
        </template>
        <template v-else>
          已有账号？
          <span class="login-link" @click="switchMode('login')">返回登录</span>
        </template>
      </p>
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
const error = ref('');
const successMsg = ref('');
const loading = ref(false);

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const PASSWORD_RE = /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$/;

function switchMode(m) {
  mode.value = m;
  error.value = '';
  successMsg.value = '';
}

function validate() {
  if (mode.value === 'register' && !username.value.trim()) {
    error.value = '请输入用户名';
    return false;
  }
  if (!email.value.trim()) {
    error.value = '请输入邮箱';
    return false;
  }
  if (!EMAIL_RE.test(email.value)) {
    error.value = '请输入有效的邮箱地址';
    return false;
  }
  if (!password.value) {
    error.value = '请输入密码';
    return false;
  }
  if (!PASSWORD_RE.test(password.value)) {
    error.value = '密码必须包含大小写字母和数字，至少8位';
    return false;
  }
  if (mode.value === 'register' && password.value !== confirmPassword.value) {
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
      await apiRegister(username.value.trim(), email.value, password.value);
      successMsg.value = '注册成功';
      mode.value = 'login';
      password.value = '';
      confirmPassword.value = '';
    } else {
      await apiLogin(email.value, password.value);
      emit('logged-in');
    }
  } catch (e) {
    error.value = e.message || '操作失败，请重试';
  } finally {
    loading.value = false;
  }
}
</script>
