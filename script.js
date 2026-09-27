// ---------------------------------------------------------------------------
// SET THIS to your backend's public URL (your ngrok forwarding address,
// or your real domain once you're not on ngrok anymore). No trailing slash.
// ---------------------------------------------------------------------------
const API_BASE = "https://anybody-fall-snippet.ngrok-free.dev";

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

async function postJSON(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "ngrok-skip-browser-warning": "true",
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
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }

  return data;
}

async function getJSON(path) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: {
      "ngrok-skip-browser-warning": "true",
    },
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error((data && data.detail) || `Server responded ${res.status}`);
  }
  return data;
}

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

function checkPushReady() {
  pushBtn.disabled = !pushForm.checkValidity();
}
[phone, amount, route].forEach((el) => el.addEventListener("input", checkPushReady));
checkPushReady();

function pollPaymentStatus(checkoutId, phoneShown, amountShown, attempt = 0, failCount = 0) {
  const MAX_ATTEMPTS = 60; // 60 x 3s = 3 minutes — covers slow real-world callbacks
  const MAX_CONSECUTIVE_FAILS = 3;

  getJSON(`/stk-push/${checkoutId}/status`)
    .then((data) => {
      if (data.status === "success") {
        setStatus(
          pushStatus,
          `Payment done ✓ — KES ${amountShown.toLocaleString()} from ${phoneShown}.`,
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
        () => pollPaymentStatus(checkoutId, phoneShown, amountShown, attempt + 1, 0),
        3000
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
        () => pollPaymentStatus(checkoutId, phoneShown, amountShown, attempt, failCount + 1),
        3000
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
    const data = await postJSON("/stk-push", {
      phone_number: phoneValue,
      amount: amountValue,
      route: route.value,
    });

    setStatus(pushStatus, "Prompt sent — waiting for payment…", "pending");
    pollPaymentStatus(data.checkout_request_id, phoneValue, amountValue);
  } catch (err) {
    setStatus(pushStatus, `Couldn't send the prompt — ${err.message}`, "error");
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
