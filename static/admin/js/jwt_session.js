(function() {
  'use strict';

  // v1 canon (stapel-auth urls_v1.py) — NOT the pre-v1 '/auth/api/jwt/*'
  // literal this used to hardcode. That literal 404-looped forever after
  // AUTH-02 (2026-08-24) retired the unversioned api/jwt/{refresh,status}
  // mount on a fleet host: refresh had no tracked-session requirement, so
  // it was closed there for good. Status carries no such risk (read-only:
  // decodes and reports the caller's own cookie-borne tokens, never
  // re-mints one) and is mounted back under v1 (get_jwt_status_urls);
  // refresh stays on the pre-existing, session-tracked v1 endpoint.
  var STATUS_URL = '/auth/api/v1/jwt/status/';
  var REFRESH_URL = '/auth/api/v1/token/refresh/';
  var CHECK_INTERVAL = 30000; // check every 30s
  var WARN_BEFORE = 60; // show modal 60s before expiry
  var ACTIVITY_THROTTLE = 5000; // throttle activity tracking
  // A 404 is a permanent answer, not a transient one — polling a mount
  // that isn't there forever is its own defect (804 hits/week measured on
  // one open admin tab, 2026-09-18 log audit). Stop after this many
  // *consecutive* 404s from the status endpoint.
  var MAX_CONSECUTIVE_404S = 3;

  var tokenExp = null;
  var lastActivity = Date.now();
  var modalShown = false;
  var countdownTimer = null;
  var modalEl = null;
  var pollTimer = null;
  var consecutiveStatus404s = 0;

  function stopPolling(reason) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('jwt_session: stopped polling — ' + reason);
    }
  }

  // --- Activity tracking ---
  var activityThrottled = false;
  function onActivity() {
    if (activityThrottled) return;
    lastActivity = Date.now();
    activityThrottled = true;
    setTimeout(function() { activityThrottled = false; }, ACTIVITY_THROTTLE);
  }
  document.addEventListener('click', onActivity, true);
  document.addEventListener('keydown', onActivity, true);

  // --- Fetch token status ---
  function fetchStatus(cb) {
    fetch(STATUS_URL, { credentials: 'same-origin' })
      .then(function(r) {
        if (r.status === 404) {
          consecutiveStatus404s += 1;
          if (consecutiveStatus404s >= MAX_CONSECUTIVE_404S) {
            stopPolling(
              STATUS_URL + ' answered 404 ' + consecutiveStatus404s +
              'x in a row — treating as permanently absent, not retrying'
            );
          }
          throw new Error('jwt status 404');
        }
        consecutiveStatus404s = 0;
        return r.json();
      })
      .then(function(data) {
        if (data.tokens && data.tokens.access_token_exp) {
          tokenExp = data.tokens.access_token_exp;
        }
        if (cb) cb(data);
      })
      .catch(function() {});
  }

  // --- Refresh token ---
  function refreshToken(cb) {
    fetch(REFRESH_URL, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }
    })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status === 'success') {
          // re-fetch status to get new exp
          fetchStatus(cb);
        } else if (cb) {
          cb(null);
        }
      })
      .catch(function() { if (cb) cb(null); });
  }

  // --- Modal ---
  function createModal() {
    if (modalEl) return;
    modalEl = document.createElement('div');
    modalEl.id = 'jwt-session-modal';
    modalEl.innerHTML =
      '<div style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:99999;display:flex;align-items:center;justify-content:center">' +
        '<div style="background:#fff;border-radius:8px;padding:24px 32px;max-width:400px;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,0.2)">' +
          '<h3 style="margin:0 0 12px;font-size:18px;color:#333">Session expiring</h3>' +
          '<p style="margin:0 0 16px;color:#666;font-size:14px">Due to inactivity your session will expire. Unsaved changes may be lost.</p>' +
          '<div id="jwt-countdown" style="font-size:32px;font-weight:700;color:#e65100;margin:0 0 20px"></div>' +
          '<button id="jwt-stay-btn" style="background:#417690;color:#fff;border:none;padding:10px 32px;border-radius:4px;font-size:14px;cursor:pointer;font-weight:500">I\'m here</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(modalEl);
    document.getElementById('jwt-stay-btn').addEventListener('click', function() {
      hideModal();
      lastActivity = Date.now();
      refreshToken(null);
    });
  }

  function showModal(secondsLeft) {
    if (modalShown) return;
    modalShown = true;
    createModal();
    modalEl.style.display = 'block';
    updateCountdown(secondsLeft);
    countdownTimer = setInterval(function() {
      var now = Math.floor(Date.now() / 1000);
      var left = tokenExp - now;
      if (left <= 0) {
        clearInterval(countdownTimer);
        window.location.reload();
        return;
      }
      updateCountdown(left);
    }, 1000);
  }

  function hideModal() {
    modalShown = false;
    if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
    if (modalEl) { modalEl.style.display = 'none'; }
  }

  function updateCountdown(seconds) {
    var el = document.getElementById('jwt-countdown');
    if (!el) return;
    var m = Math.floor(seconds / 60);
    var s = seconds % 60;
    el.textContent = m + ':' + (s < 10 ? '0' : '') + s;
  }

  // --- Main check loop ---
  function check() {
    if (!tokenExp) { fetchStatus(null); return; }

    var now = Math.floor(Date.now() / 1000);
    var secondsLeft = tokenExp - now;
    var wasActive = (Date.now() - lastActivity) < 120000; // active in last 2 min

    if (secondsLeft <= 0) {
      // expired — reload to trigger login redirect
      window.location.reload();
      return;
    }

    if (wasActive && secondsLeft <= 300) {
      // active user, token expiring in 5 min — silent refresh
      hideModal();
      refreshToken(null);
      return;
    }

    if (!wasActive && secondsLeft <= WARN_BEFORE) {
      // inactive, about to expire — show modal
      showModal(secondsLeft);
      return;
    }

    // all good, hide modal if somehow shown
    if (modalShown && secondsLeft > WARN_BEFORE) {
      hideModal();
    }
  }

  // --- Init ---
  fetchStatus(function() {
    pollTimer = setInterval(check, CHECK_INTERVAL);
  });
})();
