import { initializeApp } from "@firebase/app";
import {
  getAuth,
  onAuthStateChanged,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  signOut,
} from "@firebase/auth";

// ---------------------------------------------------------------------------
// Your backend's public URL (the Railway deployment). No trailing slash.
// ---------------------------------------------------------------------------
const API_BASE = "https://thibitifare-production.up.railway.app";

let firebaseAuth = null;
let currentUser = null;

// ---- shared helpers -------------------------------------------------------

function setStatus(el, text, state) {
  el.textContent = text;
  el.dataset.state = state; // "pending" | "ok" | "error"
}

function toDarajaPhone(localNumber) {
  // Converts the conductor-friendly "0712345678" into Daraja's
  // required "254712345678" format.
  return "254" + localNumber.slice(1);
}

async function postJSON(path, body, extraHeaders = {}) {
  if (!currentUser) throw new Error("Sign in to continue.");
  const idToken = await currentUser.getIdToken();
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${idToken}`,
      ...extraHeaders,
    },
    body: JSON.stringify(body),
  });

  let data = null;
  try {
    data = await res.json();
  } catch {
    // no JSON body — fine, we'll fall back to the status text
  }

  if (!res.ok) {
    const message =
      (data && (data.detail || data.message)) || `Server responded ${res.status}`;
    if (res.status === 401) {
      if (firebaseAuth) await signOut(firebaseAuth);
      throw new Error("Your sign-in expired. Please sign in again.");
    }
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }

  return data;
}

async function getJSON(path) {
  if (!currentUser) throw new Error("Sign in to continue.");
  const idToken = await currentUser.getIdToken();
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { Authorization: `Bearer ${idToken}` },
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401) {
      if (firebaseAuth) await signOut(firebaseAuth);
      throw new Error("Your sign-in expired. Please sign in again.");
    }
    throw new Error((data && data.detail) || `Server responded ${res.status}`);
  }
  return data;
}

const loginForm = document.getElementById("loginForm");
const loginEmail = document.getElementById("loginEmail");
const loginPassword = document.getElementById("loginPassword");
const loginBtn = document.getElementById("loginBtn");
const resetPasswordBtn = document.getElementById("resetPasswordBtn");
const signOutBtn = document.getElementById("signOutBtn");
const signedInControls = document.getElementById("signedInControls");
const accountStatus = document.getElementById("accountStatus");
const authStatus = document.getElementById("authStatus");
const protectedApp = document.getElementById("protectedApp");
const adminTools = document.getElementById("adminTools");
const roleForm = document.getElementById("roleForm");
const targetUid = document.getElementById("targetUid");
const targetRole = document.getElementById("targetRole");
const roleBtn = document.getElementById("roleBtn");
const roleStatus = document.getElementById("roleStatus");
const loadAuditBtn = document.getElementById("loadAuditBtn");
const auditEvents = document.getElementById("auditEvents");

function checkLoginReady() {
  loginBtn.disabled = !firebaseAuth || !loginForm.checkValidity();
  resetPasswordBtn.disabled = !firebaseAuth || !loginEmail.value.trim();
}

[loginEmail, loginPassword].forEach((element) =>
  element.addEventListener("input", checkLoginReady)
);

function showAuthError(message) {
  setStatus(authStatus, message, "error");
}

function updateSignedInView(user, role) {
  const previousUser = currentUser;
  currentUser = user;
  if (!user || (previousUser && previousUser.uid !== user.uid)) {
    stopTimer(pushTimer);
    stopTimer(verifyTimer);
    pushIdempotencyKey = null;
    roleForm.reset();
    auditEvents.replaceChildren();
  }
  loginForm.hidden = Boolean(user);
  signedInControls.hidden = !user;
  const authorized = role === "admin" || role === "conductor";
  protectedApp.hidden = !authorized;
  adminTools.hidden = role !== "admin";
  if (!user) {
    setStatus(authStatus, "Sign in with your conductor account to continue.", "idle");
    checkRoleReady();
    return;
  }
  if (!authorized) {
    setStatus(accountStatus, "This account has not been authorized. Contact your administrator.", "error");
    checkRoleReady();
    return;
  }
  setStatus(accountStatus, `Signed in as ${user.email || "conductor"}.`, "ok");
  setStatus(authStatus, "", "idle");
  checkRoleReady();
}

function checkRoleReady() {
  roleBtn.disabled = !currentUser || !roleForm.checkValidity();
}

targetUid.addEventListener("input", checkRoleReady);

roleForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!roleForm.checkValidity()) return;
  roleBtn.disabled = true;
  setStatus(roleStatus, "Updating account role…", "pending");
  try {
    await postJSON(
      `/admin/users/${encodeURIComponent(targetUid.value.trim())}/role`,
      { role: targetRole.value }
    );
    setStatus(
      roleStatus,
      "Access updated. The user must sign in again for the change to take effect.",
      "ok"
    );
    roleForm.reset();
  } catch (error) {
    setStatus(roleStatus, `Role update failed — ${error.message}`, "error");
  } finally {
    checkRoleReady();
  }
});

loadAuditBtn.addEventListener("click", async () => {
  loadAuditBtn.disabled = true;
  try {
    const data = await getJSON("/admin/audit-events?limit=20");
    auditEvents.replaceChildren();
    for (const event of data.events) {
      const item = document.createElement("li");
      item.textContent =
        `${event.created_at} — ${event.action} — ${event.actor_uid}` +
        `${event.subject_uid ? ` → ${event.subject_uid}` : ""} — HTTP ${event.response_status}`;
      auditEvents.append(item);
    }
    if (!data.events.length) {
      const item = document.createElement("li");
      item.textContent = "No activity recorded yet.";
      auditEvents.append(item);
    }
  } catch (error) {
    setStatus(authStatus, `Audit log unavailable — ${error.message}`, "error");
  } finally {
    loadAuditBtn.disabled = false;
  }
});

async function initializeAuthentication() {
  try {
    const response = await fetch(`${API_BASE}/auth/config`);
    if (!response.ok) throw new Error("Could not load sign-in configuration.");
    const config = await response.json();
    if (!config.enabled) {
      showAuthError("Sign-in is not configured on the server yet.");
      loginForm.hidden = true;
      return;
    }

    firebaseAuth = getAuth(initializeApp(config.firebase));
    checkLoginReady();
    onAuthStateChanged(firebaseAuth, async (user) => {
      if (!user) {
        updateSignedInView(null, null);
        return;
      }
      try {
        const token = await user.getIdTokenResult();
        if (firebaseAuth.currentUser?.uid !== user.uid) return;
        updateSignedInView(user, token.claims.role);
      } catch {
        updateSignedInView(null, null);
        showAuthError("Could not confirm your account permissions. Please sign in again.");
        await signOut(firebaseAuth);
      }
    });
  } catch {
    showAuthError("Sign-in could not be initialized. Please try again later.");
    loginForm.hidden = true;
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!firebaseAuth || !loginForm.checkValidity()) return;
  loginBtn.disabled = true;
  setStatus(authStatus, "Signing in…", "pending");
  try {
    await signInWithEmailAndPassword(
      firebaseAuth,
      loginEmail.value.trim(),
      loginPassword.value
    );
    loginPassword.value = "";
  } catch {
    showAuthError("Sign-in failed. Check your email and password, then try again.");
  } finally {
    checkLoginReady();
  }
});

resetPasswordBtn.addEventListener("click", async () => {
  if (!firebaseAuth || !loginEmail.value.trim()) {
    showAuthError("Enter your account email first.");
    return;
  }
  try {
    await sendPasswordResetEmail(firebaseAuth, loginEmail.value.trim());
    setStatus(authStatus, "If an account exists for that email, a reset link has been sent.", "ok");
  } catch {
    showAuthError("Password reset could not be started. Check the email and try again.");
  }
});

signOutBtn.addEventListener("click", async () => {
  if (!firebaseAuth) return;
  try {
    await signOut(firebaseAuth);
    stopTimer(pushTimer);
    stopTimer(verifyTimer);
    pushIdempotencyKey = null;
  } catch {
    showAuthError("Sign-out failed. Please try again.");
  }
});

void initializeAuthentication();

function stopTimer(timerRef) {
  if (timerRef.id) clearTimeout(timerRef.id);
  timerRef.id = null;
}

// ---- send payment prompt ---------------------------------------------------

const pushForm = document.getElementById("pushForm");
const phone = document.getElementById("phone");
const amount = document.getElementById("amount");
const route = document.getElementById("route");
const pushBtn = document.getElementById("pushBtn");
const pushStatus = document.getElementById("pushStatus");

const pushTimer = { id: null };
let pushIdempotencyKey = null;

function checkPushReady() {
  pushBtn.disabled = !pushForm.checkValidity();
}
[phone, amount, route].forEach((el) => el.addEventListener("input", checkPushReady));
checkPushReady();

function pollPaymentStatus(checkoutId, amountShown, attempt = 0, failCount = 0) {
  const MAX_ATTEMPTS = 36; // 36 x 5s = 3 minutes — covers slow real-world callbacks
  const MAX_CONSECUTIVE_FAILS = 3;

  getJSON(`/stk-push/${checkoutId}/status`)
    .then((data) => {
      if (data.status === "success") {
        setStatus(
          pushStatus,
          `Payment done ✓ — KES ${amountShown.toLocaleString()}.`,
          "ok"
        );
        stopTimer(pushTimer);
        pushForm.reset();
        checkPushReady();
        return;
      }
      if (data.status === "failed") {
        setStatus(pushStatus, `Payment failed — ${data.reason || "please try again."}`, "error");
        stopTimer(pushTimer);
        checkPushReady();
        return;
      }
      if (attempt >= MAX_ATTEMPTS) {
        setStatus(
          pushStatus,
          "Still waiting — ask the passenger to check their phone, or verify manually below.",
          "error"
        );
        stopTimer(pushTimer);
        checkPushReady();
        return;
      }
      setStatus(pushStatus, "Waiting for payment…", "pending");
      pushTimer.id = setTimeout(
        () => pollPaymentStatus(checkoutId, amountShown, attempt + 1, 0),
        5000
      );
    })
    .catch(() => {
      if (failCount + 1 >= MAX_CONSECUTIVE_FAILS) {
        setStatus(
          pushStatus,
          "Lost track of the payment — check your connection and verify manually below.",
          "error"
        );
        stopTimer(pushTimer);
        checkPushReady();
        return;
      }
      // transient network blip — retry without alarming the conductor
      pushTimer.id = setTimeout(
        () => pollPaymentStatus(checkoutId, amountShown, attempt, failCount + 1),
        5000
      );
    });
}

pushForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!pushForm.checkValidity()) {
    setStatus(pushStatus, "Please check the fields above.", "error");
    return;
  }

  stopTimer(pushTimer);
  pushBtn.disabled = true;
  setStatus(pushStatus, "Sending…", "pending");

  const phoneValue = phone.value;
  const amountValue = Number(amount.value);

  try {
    if (!pushIdempotencyKey) pushIdempotencyKey = crypto.randomUUID();
    const data = await postJSON("/stk-push", {
      phone_number: phoneValue,
      amount: amountValue,
      route: route.value,
    }, { "Idempotency-Key": pushIdempotencyKey });

    pushIdempotencyKey = null;
    setStatus(pushStatus, "Prompt sent — waiting for payment…", "pending");
    pollPaymentStatus(data.checkout_request_id, amountValue);
  } catch (err) {
    setStatus(pushStatus, `Couldn't confirm the prompt request — ${err.message}`, "error");
    checkPushReady();
  }
});

// ---- verify a payment -------------------------------------------------------

const verifyForm = document.getElementById("verifyForm");
const txCode = document.getElementById("txCode");
const expectedAmount = document.getElementById("expectedAmount");
const verifyBtn = document.getElementById("verifyBtn");
const verifyStatus = document.getElementById("verifyStatus");

const verifyTimer = { id: null };

function checkVerifyReady() {
  verifyBtn.disabled = !verifyForm.checkValidity();
}
[txCode, expectedAmount].forEach((el) => el.addEventListener("input", checkVerifyReady));
checkVerifyReady();

function describeVerifyResult(data) {
  const result = (data && data.result) || "";
  switch (result) {
    case "verified":
      return { text: "Verified — the code matches a real payment.", state: "ok" };
    case "already_used":
      return { text: "That code has already been used for another passenger.", state: "error" };
    case "not_found":
      return { text: "No matching payment found for that code.", state: "error" };
    case "amount_mismatch":
      return {
        text: `That code exists, but the amount doesn't match (expected KES ${data.expected}).`,
        state: "error",
      };
    case "failed":
      return { text: "Safaricom could not verify this transaction.", state: "error" };
    default:
      return { text: data && data.detail ? data.detail : "Checked — see result above.", state: "ok" };
  }
}

function pollVerifyStatus(transactionId, attempt = 0, failCount = 0) {
  const MAX_ATTEMPTS = 40; // 40 x 3s = 2 minutes — TransactionStatusQuery is usually faster than STK push
  const MAX_CONSECUTIVE_FAILS = 3;

  getJSON(`/verify/${transactionId}/status`)
    .then((data) => {
      if (data.status && data.status !== "pending") {
        const { text, state } = describeVerifyResult({
          result: data.status === "amount_mismatch" ? "amount_mismatch" : data.status,
          expected: data.expected,
          detail: data.reason,
        });
        setStatus(verifyStatus, text, state);
        stopTimer(verifyTimer);
        checkVerifyReady();
        return;
      }
      if (attempt >= MAX_ATTEMPTS) {
        setStatus(verifyStatus, "Still checking with Safaricom — try again shortly.", "error");
        stopTimer(verifyTimer);
        checkVerifyReady();
        return;
      }
      setStatus(verifyStatus, "Checking with Safaricom…", "pending");
      verifyTimer.id = setTimeout(() => pollVerifyStatus(transactionId, attempt + 1, 0), 3000);
    })
    .catch(() => {
      if (failCount + 1 >= MAX_CONSECUTIVE_FAILS) {
        setStatus(verifyStatus, "Lost track of the check — check your connection and try again.", "error");
        stopTimer(verifyTimer);
        checkVerifyReady();
        return;
      }
      verifyTimer.id = setTimeout(() => pollVerifyStatus(transactionId, attempt, failCount + 1), 3000);
    });
}

verifyForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!verifyForm.checkValidity()) {
    setStatus(verifyStatus, "Please check the fields above.", "error");
    return;
  }

  stopTimer(verifyTimer);
  verifyBtn.disabled = true;
  setStatus(verifyStatus, "Checking…", "pending");

  try {
    const data = await postJSON("/verify", {
      transaction_code: txCode.value.trim().toUpperCase(),
      amount: Number(expectedAmount.value),
    });

    if (data.result === "pending") {
      // Real (non-stub) verification — Safaricom will answer asynchronously.
      setStatus(verifyStatus, "Checking with Safaricom…", "pending");
      pollVerifyStatus(data.transaction_id);
      return;
    }

    const { text, state } = describeVerifyResult(data);
    setStatus(verifyStatus, text, state);
  } catch (err) {
    setStatus(verifyStatus, `Couldn't verify — ${err.message}`, "error");
  } finally {
    checkVerifyReady();
  }
});

// ---- register the service worker (PWA) --------------------------------------

const isLocalDev = ["localhost", "127.0.0.1"].includes(window.location.hostname);
if ("serviceWorker" in navigator && !isLocalDev) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/Service-Worker.js").catch(() => {});
  });
}
