// Chat widget shared by the staff assistant and the public chatbot.
// Everything from the server is inserted with textContent (never innerHTML): model and tool output is untrusted.
function initChat(opts) {
  const log = document.getElementById('log');
  const form = document.getElementById('chatForm');
  const input = document.getElementById('msg');
  const send = document.getElementById('sendBtn');

  function add(role, text) {
    document.getElementById('hint')?.remove();
    const row = document.createElement('div');
    row.className = 'msg ' + role;
    const b = document.createElement('div');
    b.className = 'bubble';
    b.textContent = text;
    row.appendChild(b);
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
    return row;
  }

  // restore server-rendered history safely
  log.querySelectorAll('.msg').forEach(row => {
    const data = row.querySelector('script[type="application/json"]');
    if (data) { row.querySelector('.bubble').textContent = JSON.parse(data.textContent); data.remove(); }
  });
  log.scrollTop = log.scrollHeight;

  function label(k) { return k.endsWith('_inr') ? label(k.slice(0, -4)) + ' (INR)' : k.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()); }
  function fmt(v) {
    if (v === null || v === undefined) return '—';
    if (Array.isArray(v)) return v.length ? v.map(x => typeof x === 'object' ? JSON.stringify(x) : x).join(', ') : 'none';
    if (typeof v === 'object') return Object.entries(v).map(([a, b]) => label(a) + ': ' + JSON.stringify(b)).join('; ');
    if (typeof v === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(v))
      return new Date(v).toLocaleString('en-IN', {weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit'});
    return String(v);
  }

  function card(c) {
    const box = document.createElement('div');
    box.className = 'confirm-card card border-warning my-2';
    const head = document.createElement('div');
    head.className = 'card-header bg-warning-subtle small fw-semibold';
    head.textContent = 'Confirm: ' + c.title;
    const body = document.createElement('div');
    body.className = 'card-body small';
    const dl = document.createElement('dl');
    dl.className = 'row mb-2';
    Object.entries(c.preview || {}).filter(([k]) => k !== 'action').forEach(([k, v]) => {
      const dt = document.createElement('dt'); dt.className = 'col-sm-4'; dt.textContent = label(k);
      const dd = document.createElement('dd'); dd.className = 'col-sm-8'; dd.textContent = fmt(v);
      dl.append(dt, dd);
    });
    const yes = document.createElement('button'); yes.className = 'btn btn-sm btn-success me-2'; yes.textContent = 'Confirm';
    const no = document.createElement('button'); no.className = 'btn btn-sm btn-outline-secondary'; no.textContent = 'Cancel';
    async function decide(decision) {
      yes.disabled = no.disabled = true;
      const r = await fetch(opts.confirmBase + c.id, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': CSRF},
                                                       body: JSON.stringify({decision})});
      const j = await r.json().catch(() => ({message: 'Something went wrong.'}));
      box.classList.replace('border-warning', j.ok && decision === 'confirm' ? 'border-success' : 'border-secondary');
      add('assistant', j.message + (j.hint ? ' ' + j.hint : ''));
    }
    yes.onclick = () => decide('confirm');
    no.onclick = () => decide('cancel');
    body.append(dl, yes, no);
    box.append(head, body);
    log.appendChild(box);
    log.scrollTop = log.scrollHeight;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    add('user', text);
    const typing = add('assistant', '…');
    send.disabled = true;
    try {
      const r = await fetch(opts.endpoint, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': CSRF},
                                            body: JSON.stringify({message: text})});
      const j = await r.json();
      typing.remove();
      add('assistant', j.reply || j.error || 'No answer.');
      if (j.card) card(j.card);
    } catch (err) {
      typing.remove();
      add('assistant', 'Network error. Please try again.');
    }
    send.disabled = false;
    input.focus();
  });

  document.getElementById('resetBtn')?.addEventListener('click', async () => {
    await fetch(opts.resetUrl, {method: 'POST', headers: {'X-CSRF-Token': CSRF}});
    log.innerHTML = '';
  });
}
