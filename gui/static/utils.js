function showToast(message, type) {
  var container = document.getElementById('toast-container');
  if (!container) return;
  var alert = document.createElement('div');
  alert.className = 'alert alert-' + (type || 'info') + ' shadow-sm';
  alert.setAttribute('role', 'alert');
  alert.innerHTML = '<span>' + message + '</span>';
  container.appendChild(alert);
  setTimeout(function () {
    alert.style.transition = 'opacity 0.5s';
    alert.style.opacity = '0';
    setTimeout(function () { alert.remove(); }, 500);
  }, 4000);
}
