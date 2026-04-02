// ── Heartbeat: keep desktop server alive while tab is open ──────────────
(function() {
  var hbTimer = setInterval(function() {
    fetch('/api/heartbeat').catch(function(){});
  }, 3000);

  function sendShutdown() {
    navigator.sendBeacon('/api/shutdown');
  }
  window.addEventListener('beforeunload', sendShutdown);
  window.addEventListener('pagehide',     sendShutdown);
})();
