/* Демо «Дирижёра» для GitHub Pages: подмена связи с прибором.
   Страница та же, что на приборе. Здесь запросы /api/… не уходят в сеть, а отвечаются из записей
   demo/*.json, которые сделал tools/build_pages.py настоящим ядром на учебной микросхеме.
   Задания проигрываются по дереву ходов: ответ человека выбирает ветку, как на приборе.
   Сохранение, капот и «что помогло» в демо не работают — страница об этом говорит. */
(function () {
  "use strict";
  const realFetch = window.fetch.bind(window);
  const load = name => realFetch("demo/" + name + ".json").then(r => { if (!r.ok) throw new Error(name); return r.json(); });
  const CORE = load("core");
  let JOBS = null, HOOD = null;
  const jobs = () => JOBS || (JOBS = load("jobs"));
  const hood = () => HOOD || (HOOD = load("hood"));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const NOT_HERE = "Демо: это работает только на приборе — здесь нет сервера и железа.";

  const D = { state: null, job: null, qcount: 0 };

  function reply(obj, status) {
    return new Response(JSON.stringify(obj), { status: status || 200, headers: { "content-type": "application/json; charset=utf-8" } });
  }
  const fail = (status, text) => reply({ error: text }, status);

  /* ---------- задания: проигрывание дерева ходов ---------- */
  function newJob(title, action, params, sim) {
    D.qcount = 0;
    return { id: Math.random().toString(16).slice(2, 10), title, action, params, sim, dry_run: !!params.dry_run,
      events: [], seq: 0, pending: [], question: null, result: null, next: [], done: false, node: null,
      started: Date.now() / 1000 };
  }
  function push(J, kind, text, extra) { J.seq++; J.events.push(Object.assign({ seq: J.seq, kind, text }, extra || {})); }
  function finish(J, result, next) { J.result = result; J.next = next || []; J.done = true; J.seq++; }
  function enter(J, node) { J.node = node; J.pending = (node.ev || []).slice(); }
  function step(J) {
    if (J.done || J.question) return;
    for (let i = 0; i < 3 && J.pending.length; i++) { J.seq++; J.events.push(Object.assign({}, J.pending.shift(), { seq: J.seq })); }
    if (J.pending.length) return;
    const node = J.node;
    if (node.q) { J.question = Object.assign({ qid: ++D.qcount }, node.q); J.seq++; return; }
    if (node.end) { D.state = node.end.state || D.state; finish(J, node.end.result, node.end.next); return; }
    finish(J, { session: null, outcome: null, outcome_text: "готово" }, []);
  }
  function state(J, since, jobId) {
    if (!J) return null;
    if (jobId !== J.id) since = 0;
    return { id: J.id, title: J.title, action: J.action, params: J.params, sim: J.sim, dry_run: J.dry_run,
      running: !J.done, done: J.done, seq: J.seq, events: J.events.filter(e => e.seq > since),
      question: J.question, result: J.result, next: J.next, started: J.started };
  }
  function answerKey(q, value) {
    if (q.type === "pause") return "ok";
    if (q.type === "yes_no") return String(value);
    if (q.type === "choose") return String(value);
    if (q.type === "confirm_word") {
      const v = String(value || "").trim().toLowerCase();
      return v === String(q.word || "ДА").toLowerCase() || v === "yes" ? "ok" : "wrong";
    }
    return "ok";
  }
  const KEY_RU = { ok: "подтверждено", wrong: "неверное слово", yes: "да", no: "нет", later: "позже" };

  async function startJob(body) {
    const action = String(body.action || ""), params = body.params || {};
    const key = action + (params.dry_run ? ":dry" : "") + (action === "sim" ? ":" + params.do : "");
    const all = await jobs();
    const core = await CORE;
    if (D.state === null) D.state = all.start;
    const sim = action === "sim" ? true : !!body.sim;
    const tree = sim ? (all.jobs[D.state] || {})[key] : null;
    const J = newJob(tree ? tree.title : "Задание", action, params, sim);
    D.job = J;
    if (!tree) {
      push(J, "warn", sim ? "Демо: этот вариант задания не записан. " + NOT_HERE
        : "Демо: железа здесь нет. Переключите режим вверху на «Учебный» — там записаны настоящие ходы ядра.");
      finish(J, { session: null, outcome: null, outcome_text: "недоступно в демо" }, []);
    } else enter(J, tree.root);
    return reply({ job: state(J, 0, J.id) });
  }

  async function getJob(url) {
    const J = D.job;
    if (!J) return reply({ job: null });
    const since = +(url.searchParams.get("since") || 0), wait = url.searchParams.get("wait") === "1";
    if (wait && !J.done) {
      if (J.question) await sleep(500);
      else { await sleep(J.pending.length ? 110 : 60); step(J); }
    }
    return reply({ job: state(J, since, url.searchParams.get("job")) });
  }

  function answer(body) {
    const J = D.job;
    if (!J || J.done || !J.question || J.question.qid !== body.qid) return fail(409, "на этот вопрос уже ответили");
    const q = J.question, node = J.node, br = node.br || {};
    const key = answerKey(q, body.value);
    let next = br[key];
    J.question = null;
    if (!next) {
      const alt = Object.keys(br)[0];
      if (!alt) { finish(J, { session: null, outcome: null, outcome_text: "готово" }, []); return reply({ ok: true }); }
      push(J, "info", "Демо: ход для ответа «" + (KEY_RU[key] || key) + "» не записан — дальше показан ход для ответа «" +
        (KEY_RU[alt] || alt) + "». На приборе ваш ответ будет учтён.");
      next = br[alt];
    }
    enter(J, next);
    return reply({ ok: true });
  }

  function cancel() {
    const J = D.job;
    if (!J || J.done || !J.question) return reply({ ok: false });
    const q = J.question;
    J.question = null;
    const br = (J.node.br || {});
    const safe = q.type === "yes_no" ? (q.options.some(o => o.value === "later") ? "later" : "no") : q.type === "confirm_word" ? "wrong" : null;
    if (safe && br[safe]) { enter(J, br[safe]); return reply({ ok: true }); }
    push(J, "warn", "остановлено. Все сделанные копии сохранены.");
    finish(J, { session: null, outcome: "interrupted", outcome_text: "прервано" }, []);
    return reply({ ok: true });
  }

  /* ---------- справка: поиск как на приборе (workshop/helpdocs.search) ---------- */
  function helpSearch(items, q, section) {
    const words = String(q || "").toLowerCase().split(/\s+/).filter(Boolean);
    const order = { article: 0, card: 1 };
    const found = [];
    for (const it of items) {
      const hay = [it.title, it.summary, (it.tags || []).join(" "), it._text || ""].join(" ").toLowerCase();
      if (!words.every(w => hay.indexOf(w) >= 0)) continue;
      const score = words.filter(w => it.title.toLowerCase().indexOf(w) >= 0).length * 3;
      found.push([section && it.section === section ? 0 : 1, order[it.kind] !== undefined ? order[it.kind] : 2, -score, it.title, it]);
    }
    found.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2] || (a[3] < b[3] ? -1 : a[3] > b[3] ? 1 : 0));
    return found.map(x => { const o = Object.assign({}, x[4]); delete o._text; return o; });
  }

  /* ---------- конструктор карточки: предпросмотр без сервера (на приборе проверяет сервер) ---------- */
  function cardPreview(F, core) {
    const lines = v => (Array.isArray(v) ? v : String(v || "").split("\n")).map(s => String(s).trim()).filter(Boolean);
    const errors = [];
    if (!/^[a-z][a-z0-9_]{1,39}$/.test(F.id || "")) errors.push("id — латиница, цифры и _, с буквы (например ch341a)");
    if (!String(F.title || "").trim()) errors.push("не заполнено название (title)");
    if (!String(F.tagline || "").trim()) errors.push("не заполнена строка «для чего» (tagline)");
    if (core.cards[F.id]) errors.push("id «" + F.id + "» уже занят");
    const kind = F.kind === "engine" ? "engine" : "adapter";
    const groups = core.catalog.groups[kind];
    const group = groups[F.group] ? F.group : Object.keys(groups)[0];
    const card = { id: F.id, kind, kind_title: core.catalog.kinds[kind], title: String(F.title || ""), tagline: String(F.tagline || ""),
      group, group_title: groups[group], icon: F.icon || "usb", what: F.what || "", voltage: F.voltage || "", usb: F.usb || "",
      license: F.license || "", since: "", lang: "", plugin: F.plugin || "", program: F.program || "", install: F.install || "",
      check: F.check || "", notes: "", lesson: "", price: "", path: "", source: "демо",
      strengths: lines(F.strengths), in_dirizher: lines(F.in_dirizher), caution: lines(F.caution), verify: lines(F.verify),
      how: lines(F.how).map(t => ({ text: t, code: "", tip: "" })), steps: lines(F.steps).map(t => ({ text: t, code: "", tip: "" })),
      targets: [], adapters: F.adapters || [], engines: F.engines || [], needs: F.needs || [],
      authors: lines(F.authors).map(l => { const p = l.split("—"); return { name: p[0].trim(), role: (p[1] || "").trim() }; }),
      links: F.link ? [{ title: "Сайт проекта", url: F.link }] : [],
      examples: lines(F.examples).map(l => { const p = l.split("#"); return { title: "", cmd: p[0].trim(), note: (p[1] || "").trim() }; }),
      drawings: [], status: "", status_level: "info",
      warnings: ["Демо: на приборе карточку проверит сервер (как её загрузит справочник) и сохранит через капот."] };
    const wires = (F.wires || []).filter(w => w.a || w.b).map(w => ({ a: w.a || "?", b: w.b || "?", color: w.color || "gray", note: w.note || "" }));
    if (wires.length) card.drawings.push({ type: "wiring", title: "Подключение", caption: "", a_title: F.a_title || "Переходник", b_title: F.b_title || "Устройство", wires });
    const yaml = "# Демо: так поля уйдут на прибор (это JSON — он тоже читается как YAML)\n" +
      JSON.stringify({ id: F.id, kind, title: F.title, tagline: F.tagline, group, icon: card.icon, what: card.what,
        steps: lines(F.steps), drawings: card.drawings, caution: card.caution, verify: card.verify }, null, 2);
    return { card: errors.length ? null : card, yaml, errors };
  }

  /* ---------- маршруты ---------- */
  async function route(method, url, body) {
    const p = url.pathname, q = k => url.searchParams.get(k);
    if (method === "POST") {
      if (p === "/api/job") return startJob(body);
      if (p === "/api/answer") return answer(body);
      if (p === "/api/cancel") return cancel();
      if (p === "/api/login") return reply({ ok: true });
      if (p === "/api/catalog/preview") return reply(cardPreview(body.fields || {}, await CORE));
      if (p === "/api/tool/check" || p === "/api/tool/save") return fail(403, "Демо: настройки можно только смотреть. Проверить и сохранить их можно на самом приборе («Прибор» → «Инструменты» → «Настроить»).");
      if (p === "/api/hood/check") return reply({ errors: [], warnings: [NOT_HERE], hints: [], needs_word: false });
      if (p === "/api/upload") return fail(403, NOT_HERE);
      return fail(403, NOT_HERE);
    }
    const core = await CORE;
    switch (p) {
      case "/api/hello": return reply(core.hello);
      case "/api/guide": return reply(core.guide);
      case "/api/suggest": { const all = await jobs(); const st = D.state || all.start; return reply(core.suggest[st] || core.suggest[all.start]); }
      case "/api/job": return getJob(url);
      case "/api/history": return reply(core.history);
      case "/api/session": return core.sessions[q("id")] ? reply(core.sessions[q("id")]) : fail(404, "нет такого сеанса");
      case "/api/pick": return reply(core.pick[q("purpose")] || { items: [] });
      case "/api/params": return core.params ? reply(core.params) : fail(404, "нет прочитанных параметров");
      case "/api/fixes": return reply(core.fixes[q("session")] || { session: +q("session"), signature: null, hints: [] });
      case "/api/device": return reply(core.device);
      case "/api/roadmap": return reply(core.roadmap);
      case "/api/images": return reply({ items: [] });
      case "/api/builder/fields": return reply(core.builder_fields);
      case "/api/builder/drafts": return reply(core.builder_drafts);
      case "/api/skills": return reply(core.skills);
      case "/api/help": return reply({ items: helpSearch(core.help_items, q("q"), q("section")).slice(0, 80), hood: core.catalog.hood });
      case "/api/help/article": return core.articles[q("id")] ? reply(core.articles[q("id")]) : fail(404, "нет такой статьи");
      case "/api/doctor": return reply(core.doctor);
      case "/api/tool": return (core.tools || {})[q("name")] ? reply(core.tools[q("name")]) : fail(404, "Демо: настройки этого инструмента смотрите на приборе");
      case "/api/catalog": return reply(core.catalog);
      case "/api/connect": return q("card") ? fail(403, "Демо: проверки подключения (USB, права, программы) идут на самом приборе — здесь нет вашего переходника") : reply(core.connect || { adapters: [] });
      case "/api/catalog/card": return core.cards[q("id")] ? reply(core.cards[q("id")]) : fail(404, "нет такой карточки");
      case "/api/hood": { const hd = await hood(); return reply(hd.areas[q("area")] || hd.areas.dev); }
      case "/api/hood/file": { const hd = await hood(); return hd.files[q("path")] ? reply(hd.files[q("path")]) : fail(404, "нет такого файла в демо"); }
      case "/api/hood/history": return reply({ path: q("path"), items: [] });
      case "/api/download": case "/api/journal": return fail(403, NOT_HERE);
    }
    return fail(404, "Демо: такого запроса нет в записи");
  }

  window.fetch = async function (input, init) {
    const raw = typeof input === "string" ? input : (input && input.url) || "";
    if (raw.indexOf("/api/") !== 0) return realFetch(input, init);
    const url = new URL(raw, "http://demo.local");
    const method = ((init && init.method) || "GET").toUpperCase();
    let body = {};
    if (init && init.body) { try { body = JSON.parse(init.body); } catch (e) { body = {}; } }
    try { return await route(method, url, body); }
    catch (e) { return fail(500, "Демо: не удалось загрузить записи (" + e.message + ")"); }
  };
  /* скачивание дампа и загрузка образа — только на приборе */
  document.addEventListener("click", ev => {
    const a = ev.target.closest && ev.target.closest("a[href^='/api/']");
    if (a) { ev.preventDefault(); alertDemo(); }
  }, true);
  function alertDemo() { const t = document.getElementById("toast"); if (t) { t.textContent = NOT_HERE; t.className = "toast"; t.hidden = false; setTimeout(() => { t.hidden = true; }, 4000); } }
})();
