import { initializeApp } from "@firebase/app";
import {
  createUserWithEmailAndPassword,
  getAuth,
  onAuthStateChanged,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  signOut,
} from "@firebase/auth";

// Backend public URL (Render). Override at build time with VITE_API_BASE_URL.
// No trailing slash.
const API_BASE = (
  import.meta.env.VITE_API_BASE_URL || "https://thibitifare.onrender.com"
).replace(/\/+$/, "");

let firebaseAuth = null;
let currentUser = null;

// ---- payment environment indicator ------------------------------------------

const envBanner = document.getElementById("envBanner");
// null until /  confirms the backend's mode; never assumed.
let paymentEnv = { daraja: null, verify: null };

function renderEnvBanner() {
  const { daraja, verify } = paymentEnv;
  if (!["sandbox", "production"].includes(daraja) || !["stub", "live"].includes(verify)) {
    setStatus(
      envBanner,
      "Environment unconfirmed — do not initiate payment.",
      "error"
    );
    return;
  }
  const parts = [];
  parts.push(
    daraja === "sandbox"
      ? "SANDBOX: test environment, not a real-money payment environment."
      : "PRODUCTION: payments use real money."
  );
  parts.push(
    verify === "stub"
      ? "Verification is SIMULATED (stub): it only checks locally recorded payments and is not confirmed by Safaricom."
      : "Verification is live (asked of Safaricom)."
  );
  setStatus(envBanner, parts.join(" "), daraja === "sandbox" || verify === "stub" ? "pending" : "ok");
}

function simulatedNote() {
  if (paymentEnv.daraja === "sandbox") return " (sandbox test — no real money moved)";
  if (paymentEnv.daraja !== "production") return " (environment unconfirmed)";
  return "";
}

async function loadPaymentEnvironment() {
  try {
    const res = await fetch(`${API_BASE}/`);
    const data = res.ok ? await res.json() : null;
    paymentEnv = {
      daraja: data && data.daraja_env,
      verify: data && data.verify_mode,
    };
  } catch {
    paymentEnv = { daraja: null, verify: null };
  }
  renderEnvBanner();
}

void loadPaymentEnvironment();

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
    const error = new Error(typeof message === "string" ? message : JSON.stringify(message));
    error.status = res.status;
    throw error;
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
    const error = new Error((data && data.detail) || `Server responded ${res.status}`);
    error.status = res.status;
    throw error;
  }
  return data;
}

const loginForm = document.getElementById("loginForm");
const loginEmail = document.getElementById("loginEmail");
const loginPassword = document.getElementById("loginPassword");
const loginBtn = document.getElementById("loginBtn");
const signedOutControls = document.getElementById("signedOutControls");
const signupForm = document.getElementById("signupForm");
const signupEmail = document.getElementById("signupEmail");
const signupPassword = document.getElementById("signupPassword");
const signupConfirm = document.getElementById("signupConfirm");
const signupBtn = document.getElementById("signupBtn");
const showSignupBtn = document.getElementById("showSignupBtn");
const showLoginBtn = document.getElementById("showLoginBtn");
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
const loadUnknownBtn = document.getElementById("loadUnknownBtn");
const unknownAttempts = document.getElementById("unknownAttempts");
const reviewForm = document.getElementById("reviewForm");
const reviewTarget = document.getElementById("reviewTarget");
const reviewEvidenceType = document.getElementById("reviewEvidenceType");
const reviewEvidenceRef = document.getElementById("reviewEvidenceRef");
const reviewReason = document.getElementById("reviewReason");
const reviewBtn = document.getElementById("reviewBtn");
const reviewCancelBtn = document.getElementById("reviewCancelBtn");
const reviewStatus = document.getElementById("reviewStatus");
let reviewingAttemptKey = null;

function resetReviewPanel() {
  reviewingAttemptKey = null;
  unknownAttempts.replaceChildren();
  reviewForm.reset();
  reviewForm.hidden = true;
  reviewStatus.textContent = "";
}

function checkLoginReady() {
  loginBtn.disabled = !firebaseAuth || !loginForm.checkValidity();
  resetPasswordBtn.disabled = !firebaseAuth || !loginEmail.value.trim();
}

[loginEmail, loginPassword].forEach((element) =>
  element.addEventListener("input", checkLoginReady)
);

function checkSignupReady() {
  signupBtn.disabled = !firebaseAuth || !signupForm.checkValidity();
}

[signupEmail, signupPassword, signupConfirm].forEach((element) =>
  element.addEventListener("input", checkSignupReady)
);

// ---- show / hide password ----------------------------------------------------

document.querySelectorAll(".password-toggle").forEach((button) => {
  button.addEventListener("click", () => {
    const input = document.getElementById(button.dataset.target);
    const reveal = input.type === "password";
    input.type = reveal ? "text" : "password";
    button.setAttribute("aria-pressed", String(reveal));
    button.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
  });
});

function hideAllPasswords() {
  document.querySelectorAll(".password-toggle").forEach((button) => {
    document.getElementById(button.dataset.target).type = "password";
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-label", "Show password");
  });
}

// ---- sign in / create account switch -----------------------------------------

function showAuthMode(mode) {
  const creating = mode === "signup";
  signupForm.hidden = !creating;
  loginForm.hidden = creating;
  setStatus(authStatus, "", "idle");
  if (creating) checkSignupReady();
  else checkLoginReady();
}

showSignupBtn.addEventListener("click", () => showAuthMode("signup"));
showLoginBtn.addEventListener("click", () => showAuthMode("login"));

function signupErrorMessage(error) {
  switch (error && error.code) {
    case "auth/email-already-in-use":
      return "An account with this email already exists. Sign in instead, or reset your password.";
    case "auth/invalid-email":
      return "That email address doesn't look right.";
    case "auth/weak-password":
      return "Choose a stronger password (at least 8 characters).";
    case "auth/operation-not-allowed":
      return "Account creation is switched off. Ask your administrator.";
    case "auth/too-many-requests":
      return "Too many attempts. Wait a few minutes and try again.";
    case "auth/network-request-failed":
      return "Network problem. Check your connection and try again.";
    default:
      return "Could not create the account. Please try again.";
  }
}

signupForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!firebaseAuth || !signupForm.checkValidity()) return;
  if (signupPassword.value !== signupConfirm.value) {
    showAuthError("The two passwords don't match.");
    return;
  }
  signupBtn.disabled = true;
  setStatus(authStatus, "Creating your account…", "pending");
  try {
    await createUserWithEmailAndPassword(
      firebaseAuth,
      signupEmail.value.trim(),
      signupPassword.value
    );
    signupForm.reset();
    hideAllPasswords();
    setStatus(
      authStatus,
      "Account created. An administrator must approve it before you can use ThibitiFare.",
      "ok"
    );
  } catch (error) {
    showAuthError(signupErrorMessage(error));
  } finally {
    checkSignupReady();
  }
});

function showAuthError(message) {
  setStatus(authStatus, message, "error");
}

function updateSignedInView(user, role) {
  const previousUser = currentUser;
  currentUser = user;
  if (!user || (previousUser && previousUser.uid !== user.uid)) {
    stopTimer(pushTimer);
    stopTimer(verifyTimer);
    discardPushAttemptFromMemory();
    roleForm.reset();
    auditEvents.replaceChildren();
    resetReviewPanel();
  }
  if (user && (!previousUser || previousUser.uid !== user.uid)) {
    void resumePushAttempt(user.uid);
  }
  signedOutControls.hidden = Boolean(user);
  signedInControls.hidden = !user;
  const authorized = role === "admin" || role === "conductor";
  protectedApp.hidden = !authorized;
  adminTools.hidden = role !== "admin";
  if (!user) {
    hideAllPasswords();
    showAuthMode("login");
    setStatus(authStatus, "Sign in with your conductor account to continue.", "idle");
    checkRoleReady();
    return;
  }
  hideAllPasswords();
  if (!authorized) {
    setStatus(authStatus, "", "idle");
    setStatus(
      accountStatus,
      `${user.email || "This account"} is waiting for administrator approval. Once approved, sign out and sign in again.`,
      "pending"
    );
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
    const target = targetUid.value.trim();
    if (target.includes("@")) {
      await postJSON("/admin/role-assignments", { email: target, role: targetRole.value });
    } else {
      await postJSON(`/admin/users/${encodeURIComponent(target)}/role`, {
        role: targetRole.value,
      });
    }
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

loadUnknownBtn.addEventListener("click", async () => {
  loadUnknownBtn.disabled = true;
  try {
    const data = await getJSON("/admin/stk-attempts/unknown?limit=50");
    unknownAttempts.replaceChildren();
    for (const attempt of data.attempts) {
      const item = document.createElement("li");
      const summary = document.createElement("span");
      summary.textContent =
        `${attempt.idempotency_key} — account ${attempt.owner_uid || "unrecorded"}` +
        ` — ${attempt.updated_at} — reviews: ${attempt.review_count}`;
      const reviewButton = document.createElement("button");
      reviewButton.type = "button";
      reviewButton.className = "secondary";
      reviewButton.textContent = "Review";
      reviewButton.addEventListener("click", () => {
        reviewingAttemptKey = attempt.idempotency_key;
        reviewTarget.textContent = `Reviewing attempt ${attempt.idempotency_key}`;
        reviewForm.hidden = false;
        reviewStatus.textContent = "";
        checkReviewReady();
        reviewEvidenceRef.focus();
      });
      item.append(summary, " ", reviewButton);
      unknownAttempts.append(item);
    }
    if (!data.attempts.length) {
      const item = document.createElement("li");
      item.textContent = "No unresolved attempts.";
      unknownAttempts.append(item);
    }
  } catch (error) {
    setStatus(authStatus, `Unresolved attempts unavailable — ${error.message}`, "error");
  } finally {
    loadUnknownBtn.disabled = false;
  }
});

function checkReviewReady() {
  reviewBtn.disabled = !currentUser || !reviewingAttemptKey || !reviewForm.checkValidity();
}

[reviewEvidenceType, reviewEvidenceRef, reviewReason].forEach((element) =>
  element.addEventListener("input", checkReviewReady)
);

reviewCancelBtn.addEventListener("click", () => {
  reviewingAttemptKey = null;
  reviewForm.reset();
  reviewForm.hidden = true;
});

reviewForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const key = reviewingAttemptKey;
  if (!key || !reviewForm.checkValidity()) return;
  reviewBtn.disabled = true;
  setStatus(reviewStatus, "Recording review…", "pending");
  try {
    await postJSON(`/admin/stk-attempts/${encodeURIComponent(key)}/review`, {
      outcome: "no_change_review",
      evidence_type: reviewEvidenceType.value,
      evidence_reference: reviewEvidenceRef.value.trim(),
      reason: reviewReason.value.trim(),
    });
    setStatus(
      reviewStatus,
      "Review recorded. The attempt is still unresolved; no payment was confirmed and no new prompt is allowed.",
      "ok"
    );
    reviewEvidenceRef.value = "";
    reviewReason.value = "";
  } catch (error) {
    setStatus(reviewStatus, `Review not recorded — ${error.message}`, "error");
  } finally {
    checkReviewReady();
  }
});

async function initializeAuthentication() {
  try {
    const response = await fetch(`${API_BASE}/auth/config`);
    if (!response.ok) throw new Error("Could not load sign-in configuration.");
    const config = await response.json();
    if (!config.enabled) {
      showAuthError("Sign-in is not configured on the server yet.");
      signedOutControls.hidden = true;
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
    signedOutControls.hidden = true;
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
  if (
    pushIdempotencyKey &&
    !window.confirm(
      "A payment prompt's outcome is unresolved. Check the passenger's phone before signing out. The attempt is kept for this account in this browser, so it resumes when you sign back in. Sign out anyway?"
    )
  ) {
    return;
  }
  try {
    await signOut(firebaseAuth);
    stopTimer(pushTimer);
    stopTimer(verifyTimer);
    discardPushAttemptFromMemory();
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
// The key stays set while an attempt's outcome is unknown, so retries with the
// same details reuse it and the server never creates a second prompt.
let pushIdempotencyKey = null;
let pushCheckoutId = null;
let pushBusy = false;

// An unresolved attempt is kept in localStorage so it survives reloads, closed
// tabs and sign-out. Only the idempotency key and checkout ID are stored (no
// phone number, amount, route or token), scoped to the signed-in user's uid.
// The server still enforces ownership and request details for every key.
const PUSH_ATTEMPT_PREFIX = "thibitifare.unresolvedPush.v1.";
const UUID_PATTERN = /^[0-9a-fA-F-]{36}$/;

// Returns {state: "none"} | {state: "ok", record} | {state: "ambiguous"}.
// Unreadable, malformed or foreign records are ambiguous, never "none".
function readStoredPushAttempt(uid) {
  try {
    const raw = localStorage.getItem(PUSH_ATTEMPT_PREFIX + uid);
    if (raw === null) return { state: "none" };
    const record = JSON.parse(raw);
    const valid =
      record &&
      record.v === 1 &&
      record.uid === uid &&
      typeof record.key === "string" &&
      UUID_PATTERN.test(record.key) &&
      (record.checkoutId === null || typeof record.checkoutId === "string");
    return valid ? { state: "ok", record } : { state: "ambiguous" };
  } catch {
    return { state: "ambiguous" };
  }
}

function writeStoredPushAttempt(uid, key, checkoutId) {
  try {
    localStorage.setItem(
      PUSH_ATTEMPT_PREFIX + uid,
      JSON.stringify({ v: 1, uid, key, checkoutId: checkoutId || null, createdAt: Date.now() })
    );
    return readStoredPushAttempt(uid).state === "ok";
  } catch {
    return false;
  }
}

// Called only with evidence: a terminal payment status for this attempt, or a
// rejection that happened before any payment could have been created.
function clearResolvedPushAttempt(uid) {
  try {
    if (uid) localStorage.removeItem(PUSH_ATTEMPT_PREFIX + uid);
  } catch {
    // ignore
  }
  pushIdempotencyKey = null;
  pushCheckoutId = null;
}

const UNRESOLVED_MESSAGE =
  "An earlier prompt's outcome is unresolved. Check the passenger's phone. Tap Send prompt with the same phone, fare and route to retry it safely, or use Verify below if they were charged.";

function showAmbiguousStorage() {
  setStatus(
    pushStatus,
    "A saved payment attempt could not be read, so a new prompt is blocked to avoid charging the passenger twice. Check the passenger's phone and use Verify below. Contact support to clear it.",
    "error"
  );
}

function adoptStoredPushAttempt(uid) {
  const stored = readStoredPushAttempt(uid);
  if (stored.state === "ok") {
    pushIdempotencyKey = stored.record.key;
    pushCheckoutId = stored.record.checkoutId;
  }
  return stored;
}

// After a reload or sign-in, reconnect to the stored attempt. Nothing here
// treats a failed lookup as proof that no prompt was sent.
async function resumePushAttempt(uid) {
  const stored = adoptStoredPushAttempt(uid);
  if (stored.state === "none") return;
  if (stored.state === "ambiguous") {
    showAmbiguousStorage();
    pushBusy = true;
    checkPushReady();
    return;
  }
  pushBusy = true;
  checkPushReady();
  setStatus(pushStatus, "Reconnecting to your earlier payment…", "pending");
  let serverState = null;
  if (!pushCheckoutId) {
    try {
      const attempt = await getJSON(`/stk-push/attempts/${pushIdempotencyKey}`);
      if (!currentUser || currentUser.uid !== uid) return;
      serverState = attempt.state || null;
      if (attempt.checkout_request_id) {
        pushCheckoutId = attempt.checkout_request_id;
        writeStoredPushAttempt(uid, pushIdempotencyKey, pushCheckoutId);
      }
    } catch {
      // fall through: the attempt stays unresolved
    }
  }
  if (!currentUser || currentUser.uid !== uid) return;
  if (pushCheckoutId) {
    pollPaymentStatus(pushCheckoutId, null, 0, 0, uid);
    return;
  }
  pushBusy = false;
  if (serverState === "unknown") {
    // No checkout ID exists, so the provider cannot be queried for this attempt.
    setStatus(
      pushStatus,
      "The server could not confirm whether this prompt reached the passenger, so it cannot be checked automatically. Ask the passenger whether they were charged and use Verify below with their M-Pesa code. Do not take a new payment for this fare until an administrator has reviewed it.",
      "error"
    );
  } else if (serverState === "submitting") {
    setStatus(
      pushStatus,
      "This prompt was still being submitted when the page closed. Wait a minute, then tap Send prompt with the same details to check it again.",
      "error"
    );
  } else {
    setStatus(pushStatus, UNRESOLVED_MESSAGE, "error");
  }
  checkPushReady();
}

// Clears only in-memory state. The stored unresolved attempt is kept so the same
// user can resume it after signing back in or reloading.
function discardPushAttemptFromMemory() {
  pushIdempotencyKey = null;
  pushCheckoutId = null;
  pushBusy = false;
  checkPushReady();
}

function checkPushReady() {
  pushBtn.disabled = pushBusy || !pushForm.checkValidity();
}

function endPushPolling() {
  stopTimer(pushTimer);
  pushBusy = false;
  checkPushReady();
}
[phone, amount, route].forEach((el) => el.addEventListener("input", checkPushReady));
checkPushReady();

function pollPaymentStatus(checkoutId, amountShown, attempt = 0, failCount = 0, ownerUid = null) {
  const MAX_ATTEMPTS = 36; // 36 x 5s = 3 minutes — covers slow real-world callbacks
  const MAX_CONSECUTIVE_FAILS = 3;
  const owner = ownerUid || (currentUser && currentUser.uid);
  const stillCurrent = () => Boolean(currentUser) && currentUser.uid === owner;

  getJSON(`/stk-push/${checkoutId}/status`)
    .then((data) => {
      if (!stillCurrent()) return;
      if (data.status === "success") {
        setStatus(
          pushStatus,
          amountShown === null
            ? `Payment done ✓${simulatedNote()}.`
            : `Payment done ✓ — KES ${amountShown.toLocaleString()}${simulatedNote()}.`,
          "ok"
        );
        stopTimer(pushTimer);
        if (amountShown !== null) pushForm.reset();
        clearResolvedPushAttempt(owner);
        endPushPolling();
        return;
      }
      if (data.status === "failed") {
        setStatus(pushStatus, `Payment failed — ${data.reason || "please try again."}`, "error");
        clearResolvedPushAttempt(owner);
        endPushPolling();
        return;
      }
      if (attempt >= MAX_ATTEMPTS) {
        setStatus(
          pushStatus,
          "Still waiting — ask the passenger to check their phone, or verify manually below. The attempt is saved; tap Send prompt with the same details to keep checking it.",
          "error"
        );
        endPushPolling();
        return;
      }
      setStatus(pushStatus, "Waiting for payment…", "pending");
      pushTimer.id = setTimeout(
        () => pollPaymentStatus(checkoutId, amountShown, attempt + 1, 0, owner),
        5000
      );
    })
    .catch((err) => {
      if (!stillCurrent()) return;
      // 502/503/504 mean the provider status query itself was unavailable. The
      // payment may still be pending, so keep tracking within the normal time
      // budget instead of counting it as a lost connection.
      if (err && [502, 503, 504].includes(err.status) && attempt < MAX_ATTEMPTS) {
        setStatus(pushStatus, "Waiting for payment… (status check delayed)", "pending");
        pushTimer.id = setTimeout(
          () => pollPaymentStatus(checkoutId, amountShown, attempt + 1, 0, owner),
          5000
        );
        return;
      }
      if (failCount + 1 >= MAX_CONSECUTIVE_FAILS) {
        setStatus(
          pushStatus,
          "Lost track of the payment — check your connection and verify manually below. The attempt is saved; reload or tap Send prompt with the same details to check it again.",
          "error"
        );
        endPushPolling();
        return;
      }
      // transient network blip — retry without alarming the conductor
      pushTimer.id = setTimeout(
        () => pollPaymentStatus(checkoutId, amountShown, attempt, failCount + 1, owner),
        5000
      );
    });
}

pushForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (pushBusy) return;
  if (!pushForm.checkValidity()) {
    setStatus(pushStatus, "Please check the fields above.", "error");
    return;
  }

  // An unresolved earlier attempt is only ever retried with its own key; the
  // server rejects (409) any retry whose details differ, so no second payment
  // can be created for it.
  const submitUid = currentUser && currentUser.uid;
  if (!submitUid) return;
  pushBusy = true;
  checkPushReady();
  const phoneValue = phone.value;
  const amountValue = Number(amount.value);

  // Another tab may have stored an attempt since this page loaded.
  const stored = adoptStoredPushAttempt(submitUid);
  if (stored.state === "ambiguous") {
    showAmbiguousStorage();
    return;
  }
  let createdNow = false;
  if (!pushIdempotencyKey) {
    const newKey = crypto.randomUUID();
    // Persist before sending; if that fails, nothing has been sent yet.
    if (!writeStoredPushAttempt(submitUid, newKey, null)) {
      pushBusy = false;
      setStatus(
        pushStatus,
        "This browser could not save the payment attempt, so no prompt was sent. Enable site storage and try again.",
        "error"
      );
      checkPushReady();
      return;
    }
    pushIdempotencyKey = newKey;
    pushCheckoutId = null;
    createdNow = true;
  }

  stopTimer(pushTimer);
  setStatus(pushStatus, "Sending…", "pending");

  try {
    const data = await postJSON("/stk-push", {
      phone_number: phoneValue,
      amount: amountValue,
      route: route.value,
    }, { "Idempotency-Key": pushIdempotencyKey });

    pushCheckoutId = data.checkout_request_id;
    writeStoredPushAttempt(submitUid, pushIdempotencyKey, pushCheckoutId);
    setStatus(pushStatus, "Prompt sent — waiting for payment…", "pending");
    pollPaymentStatus(pushCheckoutId, amountValue, 0, 0, submitUid);
  } catch (err) {
    pushBusy = false;
    // Only a validation/authorisation rejection of a brand-new attempt proves no
    // prompt was created. Everything else (409, 5xx, network errors) may follow a
    // sent request, so the key is kept and a same-key retry lets the server decide.
    const definitelyNotSent = createdNow && (err.status === 422 || err.status === 403);
    if (err.status === 409 && /still being submitted/i.test(err.message)) {
      setStatus(
        pushStatus,
        "This prompt is still being submitted. Wait a few seconds, then tap Send prompt to check again.",
        "error"
      );
    } else if (err.status === 409) {
      setStatus(
        pushStatus,
        "This request conflicts with an earlier attempt. Keep the same phone, fare and route and tap Send prompt to retry it, or use Verify below if they were charged.",
        "error"
      );
    } else if (definitelyNotSent) {
      setStatus(pushStatus, `Couldn't send the prompt — ${err.message}`, "error");
    } else {
      setStatus(
        pushStatus,
        "Couldn't confirm whether the prompt was sent. Check the passenger's phone. Tap Send prompt to safely retry the same payment, or use Verify below if they were charged.",
        "error"
      );
    }
    if (definitelyNotSent) clearResolvedPushAttempt(submitUid);
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
      if (paymentEnv.verify === "stub") {
        return {
          text: `Matched a locally recorded payment — simulated (stub) check, not confirmed by Safaricom${simulatedNote()}.`,
          state: "ok",
        };
      }
      if (paymentEnv.verify !== "live") {
        return { text: "Matched a recorded payment — environment unconfirmed.", state: "ok" };
      }
      return { text: `Verified — the code matches a payment on Safaricom's record${simulatedNote()}.`, state: "ok" };
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
