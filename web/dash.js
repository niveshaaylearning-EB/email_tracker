// Minimal stand-in for the claude.ai dashboard runtime, so the same page code runs here.
// Sources are the two JSON files the tracker writes; calcs are worked out in the browser.
window.dash = (() => {
  const SOURCES = { threads: 'data/threads.json', daily: 'data/daily.json' };
  const state = {};          // id -> {status, data, message}
  const calcs = [];          // [{id, inputs, fn}] in declaration order
  const listeners = [];
  let params = {};
  let timer = null;

  const fire = () => {
    clearTimeout(timer);
    timer = setTimeout(() => listeners.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } }), 0);
  };

  const runCalcs = () => {
    for (const c of calcs) {
      const ins = c.inputs.map((i) => state[i]);
      if (ins.some((s) => !s || s.status === 'loading')) { state[c.id] = { status: 'loading', data: null }; continue; }
      const failed = ins.find((s) => s.status !== 'ok');
      if (failed) { state[c.id] = { status: 'error', data: null, message: failed.message }; continue; }
      try { state[c.id] = { status: 'ok', data: c.fn(...ins.map((s) => s.data)) }; }
      catch (e) { console.error(e); state[c.id] = { status: 'error', data: null, message: String(e) }; }
    }
  };

  const load = async () => {
    await Promise.all(Object.entries(SOURCES).map(async ([id, url]) => {
      state[id] = { status: 'loading', data: [] };
      try {
        const r = await fetch(url, { cache: 'no-store' });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
        state[id] = { status: 'ok', data: await r.json() };
      } catch (e) {
        state[id] = { status: 'error', data: [], message: String(e.message || e) };
      }
    }));
    runCalcs();
    fire();
  };

  window.addEventListener('resize', () => fire());
  document.addEventListener('DOMContentLoaded', load);

  return {
    colors: ['#2f6fdb', '#d9822b', '#2e9e6b', '#a65bd6', '#d6455b'],
    data: (id) => state[id] || { status: 'loading', data: [] },
    calc: (id, spec) => { calcs.push({ id, inputs: spec.inputs, fn: spec.fn }); },
    loader: () => {},
    onData: (fn) => { listeners.push(fn); try { fn(); } catch (e) { console.error(e); } },
    params: () => ({ ...params }),
    setParams: (p) => { params = { ...params, ...p }; fire(); },
    resetParams: () => { params = {}; fire(); },
    setLink: (name) => { try { history.replaceState(null, '', '#' + name); } catch (e) { /* ignore */ } },
    refresh: () => load(),
  };
})();
