<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { BookOutlined, LogoutOutlined, MessageOutlined, ToolOutlined, VideoCameraOutlined } from '@ant-design/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { useIMStore } from '@/stores/im'

const auth = useAuthStore()
const im = useIMStore()
const router = useRouter()
const route = useRoute()
const displayName = computed(() => auth.user?.display_name || auth.user?.username || 'operator')
const imHeaderCollapsed = ref(false)

const activeNav = computed(() => route.name)

function handleImSidebarCollapse(event) {
  imHeaderCollapsed.value = Boolean(event.detail?.collapsed)
}

async function logout() {
  if (im.source) {
    im.source.close()
    im.source = null
  }
  im.$reset()
  await auth.logout()
  router.push('/login')
}

onMounted(() => {
  window.addEventListener('im-sidebar-collapse', handleImSidebarCollapse)
})

onUnmounted(() => {
  window.removeEventListener('im-sidebar-collapse', handleImSidebarCollapse)
})
</script>

<template>
  <div class="app-layout" :class="{ 'im-header-collapsed': imHeaderCollapsed }">
    <header class="app-topbar">
      <!-- left: brand -->
      <div class="topbar-brand">
        <div class="topbar-mark"><MessageOutlined /></div>
        <div>
          <strong>Agent IM</strong>
          <span>Multi-Agent Workspace</span>
        </div>
      </div>

      <!-- center: capsule segmented nav -->
      <nav class="topbar-nav" aria-label="主导航">
        <div class="topbar-pill">
          <RouterLink
            :to="{ name: 'chat' }"
            class="topbar-pill-seg"
            :class="{ 'topbar-pill-seg--active': activeNav === 'chat' }"
          >
            <MessageOutlined class="pill-icon" />
            <span>聊天</span>
          </RouterLink>
          <RouterLink
            :to="{ name: 'skills' }"
            class="topbar-pill-seg"
            :class="{ 'topbar-pill-seg--active': activeNav === 'skills' }"
          >
            <BookOutlined class="pill-icon" />
            <span>技能库</span>
          </RouterLink>
          <RouterLink
            :to="{ name: 'tools' }"
            class="topbar-pill-seg"
            :class="{ 'topbar-pill-seg--active': activeNav === 'tools' }"
          >
            <ToolOutlined class="pill-icon" />
            <span>工具</span>
          </RouterLink>
          <RouterLink :to="{ name: 'camera' }" class="topbar-pill-seg" :class="{ 'topbar-pill-seg--active': activeNav === 'camera' }">
            <VideoCameraOutlined class="pill-icon" />
            <span>相机</span>
          </RouterLink>
        </div>
      </nav>

      <!-- right: user area -->
      <div class="topbar-user-area">
        <a-space :size="10" class="topbar-user-shell">
          <a-avatar class="topbar-avatar">{{ displayName.slice(0, 1).toUpperCase() }}</a-avatar>
          <span class="topbar-user">{{ displayName }}</span>
          <span class="topbar-divider"></span>
          <a-button type="text" class="topbar-logout" @click="logout">
            <template #icon><LogoutOutlined /></template>
            退出
          </a-button>
        </a-space>
      </div>
    </header>
    <RouterView />
  </div>
</template>

<style scoped>
/* Three-section topbar: brand | nav (centered) | user-area */
.app-topbar {
  position: sticky;
  top: 0;
  z-index: 40;
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  column-gap: 18px;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.78), rgba(245, 250, 255, 0.58)),
    rgba(255, 255, 255, 0.48);
  border-bottom-color: rgba(255, 255, 255, 0.86);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.92),
    0 14px 38px rgba(27, 39, 66, 0.10);
  backdrop-filter: blur(22px) saturate(1.22);
  -webkit-backdrop-filter: blur(22px) saturate(1.22);
}

.topbar-brand {
  min-width: 0;
}

.topbar-brand strong {
  letter-spacing: 0;
}

.topbar-brand span {
  margin-top: 1px;
  font-weight: 650;
}

.topbar-user-area {
  display: flex;
  justify-content: flex-end;
  min-width: 0;
}

.topbar-user-shell {
  padding: 5px 7px 5px 5px;
  background: rgba(255, 255, 255, 0.54);
  border: 1px solid rgba(255, 255, 255, 0.76);
  border-radius: 999px;
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.82),
    0 8px 22px rgba(27, 39, 66, 0.06);
}

.topbar-avatar {
  color: #fff;
  background: linear-gradient(135deg, var(--accent-5), var(--accent));
  box-shadow: 0 8px 18px rgba(53, 120, 255, 0.18);
}

.topbar-divider {
  width: 1px;
  height: 18px;
  background: rgba(95, 111, 139, 0.16);
}

.topbar-logout {
  height: 30px;
  padding: 0 9px;
  border-radius: 999px;
  color: var(--muted);
  font-weight: 700;
}

.topbar-logout:hover {
  color: #b42318;
  background: rgba(255, 255, 255, 0.68);
}

/* ── capsule pill ── */
.topbar-nav {
  display: flex;
  align-items: center;
  justify-content: center;
}

.topbar-pill {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.72), rgba(246, 250, 255, 0.54)),
    rgba(255, 255, 255, 0.50);
  border: 1px solid rgba(255, 255, 255, 0.82);
  border-radius: 999px;
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.90),
    0 10px 26px rgba(27, 39, 66, 0.09);
  backdrop-filter: blur(16px) saturate(1.18);
  -webkit-backdrop-filter: blur(16px) saturate(1.18);
}

.topbar-pill-seg {
  position: relative;
  z-index: 1;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-width: 92px;
  justify-content: center;
  padding: 7px 16px;
  border-radius: 999px;
  font-size: 13.5px;
  font-weight: 700;
  color: var(--muted);
  text-decoration: none;
  transition:
    color 0.18s ease,
    background 0.18s ease,
    box-shadow 0.18s ease,
    transform 0.14s ease;
  white-space: nowrap;
  user-select: none;
}

.topbar-pill-seg:hover {
  color: var(--text);
  background: rgba(255, 255, 255, 0.66);
}

.topbar-pill-seg--active {
  color: #fff;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.18), transparent 38%),
    linear-gradient(135deg, var(--accent), var(--accent-2) 58%, var(--accent-5));
  box-shadow:
    0 8px 20px rgba(53, 120, 255, 0.30),
    inset 0 1px 0 rgba(255, 255, 255, 0.28);
  transform: translateY(-0.5px);
}

.topbar-pill-seg--active:hover {
  color: #fff;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.18), transparent 38%),
    linear-gradient(135deg, var(--accent), var(--accent-2) 58%, var(--accent-5));
}

.pill-icon {
  font-size: 14px;
}

/* ── responsive: stack vertically on narrow screens ── */
@media (max-width: 760px) {
  .app-topbar {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    height: auto;
    gap: 10px;
    padding: 12px 14px;
  }

  .topbar-brand span {
    display: none;
  }

  .topbar-nav {
    align-self: stretch;
    width: 100%;
    justify-content: stretch;
  }

  .topbar-pill {
    width: 100%;
  }

  .topbar-pill-seg {
    flex: 1 1 0;
    min-width: 0;
    padding-inline: 10px;
  }

  .topbar-user-area {
    align-self: flex-end;
  }
}
</style>
