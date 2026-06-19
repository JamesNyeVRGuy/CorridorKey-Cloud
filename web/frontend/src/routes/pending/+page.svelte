<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { refreshToken } from "$lib/auth";

  let checking = $state(false);
  let lastChecked = $state("");
  let status = $state<"pending" | "approved" | "rejected" | "checking">(
    "pending",
  );
  let queuePosition = $state(0);
  let queueTotal = $state(0);
  let serverOverloaded = $state(false);

  async function checkApproval() {
    checking = true;
    try {
      // Refresh the token first to get updated app_metadata
      const session = await refreshToken();
      if (!session) {
        // No refresh token — user needs to log in
        status = "pending";
        return;
      }

      // Check the fresh token's tier
      const token = session.access_token;
      const res = await fetch("/api/auth/me", {
        headers: { Authorization: `Bearer ${token}` },
      });

      if (res.status === 503 || res.status === 502) {
        serverOverloaded = true;
        return;
      }
      serverOverloaded = false;

      if (!res.ok) return;

      const data = await res.json();
      if (data.queue_position) queuePosition = data.queue_position;
      if (data.queue_total) queueTotal = data.queue_total;

      if (data.tier && data.tier !== "pending") {
        status = "approved";
        // Update stored user with new tier
        const stored = localStorage.getItem("ck:auth_user");
        if (stored) {
          const user = JSON.parse(stored);
          user.tier = data.tier;
          localStorage.setItem("ck:auth_user", JSON.stringify(user));
        }
        setTimeout(() => goto("/clips"), 1000);
      } else if (data.tier === "rejected") {
        status = "rejected";
      }
    } catch {
      // Network error — might be overloaded
      serverOverloaded = true;
    } finally {
      checking = false;
      lastChecked = new Date().toLocaleTimeString();
    }
  }

  onMount(() => {
    checkApproval();
    const interval = setInterval(checkApproval, 8000);
    return () => clearInterval(interval);
  });
</script>

<svelte:head>
  <title>Pending Approval — CorridorKey</title>
</svelte:head>

<div class="auth-page">
  <div class="auth-card">
    <img
      src="/Corridor_Digital_Logo.svg"
      alt="Corridor Digital"
      class="auth-logo"
    />
    <div class="logo-row">
      <h1 class="auth-title mono">CORRIDORKEY</h1>
      <span class="beta-badge mono">BETA</span>
    </div>

    {#if serverOverloaded}
      <div class="status-icon overloaded">
        <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
          <circle
            cx="24"
            cy="24"
            r="20"
            stroke="var(--accent)"
            stroke-width="2"
          />
          <path
            d="M24 16v10"
            stroke="var(--accent)"
            stroke-width="2.5"
            stroke-linecap="round"
          />
          <circle cx="24" cy="32" r="1.5" fill="var(--accent)" />
        </svg>
      </div>
      <h2 class="pending-title">High Demand</h2>
      <p class="pending-text">
        We're experiencing high traffic right now. The server is temporarily
        busy — your account is safe. We'll keep checking automatically.
      </p>
    {:else if status === "approved"}
      <div class="status-icon approved">
        <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
          <circle
            cx="24"
            cy="24"
            r="20"
            stroke="var(--state-complete)"
            stroke-width="2"
          />
          <path
            d="M16 24l6 6 10-12"
            stroke="var(--state-complete)"
            stroke-width="2.5"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
      </div>
      <h2 class="pending-title">Account Approved</h2>
      <p class="pending-text">Redirecting to dashboard...</p>
    {:else if status === "rejected"}
      <div class="status-icon rejected">
        <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
          <circle
            cx="24"
            cy="24"
            r="20"
            stroke="var(--state-error)"
            stroke-width="2"
          />
          <path
            d="M18 18l12 12M30 18l-12 12"
            stroke="var(--state-error)"
            stroke-width="2.5"
            stroke-linecap="round"
          />
        </svg>
      </div>
      <h2 class="pending-title">Account Not Approved</h2>
      <p class="pending-text">
        Your account was not approved. Contact an administrator.
      </p>
    {:else}
      <div class="status-icon pending">
        <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
          <circle
            cx="24"
            cy="24"
            r="20"
            stroke="var(--state-queued)"
            stroke-width="2"
            class="spin-circle"
          />
          <path
            d="M24 14v12l8 4"
            stroke="var(--state-queued)"
            stroke-width="2"
            stroke-linecap="round"
          />
        </svg>
      </div>
      <h2 class="pending-title">Account Pending Approval</h2>
      <p class="pending-text">
        Your account has been created. An admin will review and approve your
        access. Once approved you will receive a confirmation email.
      </p>
      {#if queuePosition > 0 && queueTotal > 0}
        <div class="queue-info mono">
          You are <span class="queue-highlight">#{queuePosition}</span> of {queueTotal}
          in the approval queue
        </div>
      {/if}
      <div class="check-status mono">
        {#if checking}
          Checking...
        {:else if lastChecked}
          Last checked: {lastChecked}
        {/if}
      </div>
    {/if}

    <div class="support-cta">
      <a
        href="https://buymeacoffee.com/dcrepublic"
        target="_blank"
        rel="noopener"
        class="support-link"
      >
        Support CorridorKey's server costs
      </a>
    </div>

    <div class="auth-footer">
      <a href="/login">Back to sign in</a>
    </div>
  </div>
</div>

<style>
  .auth-page {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    background: var(--surface-0);
    padding: var(--sp-4);
  }

  .auth-card {
    width: 100%;
    max-width: 380px;
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: var(--sp-6);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--sp-4);
    text-align: center;
  }

  .auth-logo {
    width: 120px;
    filter: drop-shadow(0 0 4px rgba(255, 242, 3, 0.15));
  }

  .logo-row {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
  }
  .auth-title {
    font-size: 11px;
    letter-spacing: 0.2em;
    color: var(--text-tertiary);
  }
  .beta-badge {
    font-size: 9px;
    letter-spacing: 0.1em;
    padding: 2px 6px;
    border-radius: 4px;
    background: rgba(255, 242, 3, 0.12);
    color: var(--accent);
    border: 1px solid rgba(255, 242, 3, 0.2);
  }

  .status-icon {
    margin: var(--sp-2) 0;
  }

  .pending-title {
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .pending-text {
    font-size: 14px;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .queue-info {
    font-size: 12px;
    color: var(--text-secondary);
    padding: var(--sp-2) var(--sp-3);
    background: var(--surface-2);
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
  }

  .queue-highlight {
    color: var(--accent);
    font-weight: 600;
  }

  .check-status {
    font-size: 11px;
    color: var(--text-tertiary);
    padding: var(--sp-1) var(--sp-3);
    background: var(--surface-2);
    border-radius: var(--radius-sm);
  }

  .spin-circle {
    animation: spin-slow 8s linear infinite;
    transform-origin: center;
  }

  @keyframes spin-slow {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
  }

  .support-cta {
    font-size: 12px;
    color: var(--text-tertiary);
  }
  .support-link {
    color: var(--text-secondary);
    transition: color 0.15s;
  }
  .support-link:hover {
    color: var(--accent);
  }

  .auth-footer {
    font-size: 13px;
  }
  .auth-footer a {
    color: var(--accent);
  }
</style>
