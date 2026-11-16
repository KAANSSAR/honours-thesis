/*
  Curriculum planner frontend. Section 3.10.

  This file draws what the engine returns and collects what the student does to
  the plan. It contains no rule, no threshold and no judgement about whether a
  placement is legal, which is the same boundary Section 3.9 draws around the
  language model: everything that decides correctness is behind the API, and
  both the model and this page reach it through the same endpoints. A bug here
  can misdraw an answer. It cannot manufacture one.

  That applies to the rearranging too. Moving a unit changes a schedule the page
  is holding, and the verdict on that schedule comes back from the validator,
  never from anything computed here.
*/

"use strict";

const state = {
  view: "plan",
  institution: "USYD",
  grouped: null,
  degree: null,
  key: null,
  failures: [],        // failed units, applied in the order they were added
  schedule: null,      // the schedule now on screen
  edited: false,       // has the student moved anything by hand
  selected: null,      // unit picked up for a swap
  lastResult: null,
  pairs: [],
  modelEnabled: null,
  thread: [],
};

const $ = (id) => document.getElementById(id);

/* Plan titles are shown as the dataset records them, with the spacing around
   the separators normalised. The dataset is left alone: a stray space is not
   worth an edit to the source of truth. */
const tidy = (text) => String(text || "")
  .replace(/\s*\|\s*/g, " | ").replace(/\s+/g, " ").trim();

async function get(path, params) {
  const url = new URL(path, location.origin);
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== "") url.searchParams.set(k, v);
  });
  return (await fetch(url, { headers: { Accept: "application/json" } })).json();
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return response.json();
}

function el(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([k, v]) => {
    if (v === null || v === undefined || v === false) return;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  });
  kids.flat().forEach((kid) => {
    if (kid === null || kid === undefined || kid === false) return;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  });
  return node;
}

function notice(kind, heading, detail, items) {
  const body = [el("h3", { text: heading })];
  if (detail) body.push(el("p", { text: detail }));
  if (items && items.length) {
    body.push(el("ul", {}, items.slice(0, 10).map((t) => el("li", { text: t }))));
    if (items.length > 10) body.push(el("p", { text: `and ${items.length - 10} more` }));
  }
  return el("div", { class: `notice ${kind}` }, body);
}

const violationText = (v) =>
  typeof v === "string" ? v : (v.detail || v.message || JSON.stringify(v));

// ---------------------------------------------------------------- views

function show(view) {
  state.view = view;
  ["plan", "compare", "ask"].forEach((name) => {
    $(`view-${name}`).hidden = name !== view;
  });
  document.querySelectorAll("nav button").forEach((button) => {
    const on = button.dataset.view === view;
    if (on) button.setAttribute("aria-current", "true");
    else button.removeAttribute("aria-current");
  });
  if (view === "compare" && !state.pairs.length) loadPairs();
  if (view === "ask" && state.modelEnabled === null) checkModel();
}

document.querySelectorAll("nav button").forEach((button) => {
  button.addEventListener("click", () => show(button.dataset.view));
});

// ---------------------------------------------------------------- plan

async function loadPlans() {
  const data = await get("/api/plans");
  state.grouped = data.grouped || {};
  fillDegrees();
}

function fillDegrees(preferDegree) {
  const byDegree = state.grouped[state.institution] || {};
  const degrees = Object.keys(byDegree).sort();
  const select = $("degree");
  select.replaceChildren(...degrees.map((d) => el("option", { value: d, text: d })));
  state.degree = degrees.includes(preferDegree) ? preferDegree : degrees[0];
  select.value = state.degree || "";
  fillCombos();
}

function fillCombos(preferKey) {
  const options = (state.grouped[state.institution] || {})[state.degree] || [];
  const select = $("combo");
  select.replaceChildren(
    ...options.map((o) => el("option", { value: o.key, text: tidy(o.title) })));
  select.disabled = options.length <= 1;
  select.closest(".field").hidden = options.length <= 1;
  const keys = options.map((o) => o.key);
  state.key = keys.includes(preferKey) ? preferKey : keys[0];
  select.value = state.key || "";
  reset();
}

function reset() {
  state.failures = [];
  state.schedule = null;
  state.edited = false;
  state.selected = null;
  drawPlan();
}

$("inst").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-inst]");
  if (!button || button.dataset.inst === state.institution) return;
  state.institution = button.dataset.inst;
  $("inst").querySelectorAll("button").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.inst === state.institution));
  });
  fillDegrees();
});

$("degree").addEventListener("change", (e) => { state.degree = e.target.value; fillCombos(); });
$("combo").addEventListener("change", (e) => { state.key = e.target.value; reset(); });
$("reset").addEventListener("click", reset);

$("add-failure").addEventListener("change", async (event) => {
  const code = event.target.value;
  event.target.value = "";
  if (!code) return;
  state.failures.push(code);
  state.selected = null;
  await drawPlan();
});

$("check").addEventListener("click", async () => {
  if (!state.schedule) return;
  const result = await post("/api/arrange", {
    institution: state.institution, key: state.key, schedule: state.schedule,
  });
  const where = $("plan-notices");
  if (!result.ok) {
    where.prepend(notice("problem", "Could not check this plan", result.error));
  } else if (result.valid) {
    where.prepend(notice("ok", "Every recorded rule is satisfied",
      `Checked against the published rules for ${instName()}.`));
  } else {
    where.prepend(notice("problem",
      `${result.violation_count} rule${result.violation_count === 1 ? "" : "s"} not satisfied`,
      null, (result.violations || []).map(violationText)));
  }
});

const instName = () =>
  state.institution === "USYD" ? "the University of Sydney" : "UTS";

function drawFailureChips() {
  const box = $("failure-list");
  if (!state.failures.length) { box.replaceChildren(); box.hidden = true; return; }
  box.hidden = false;
  const counts = {};
  box.replaceChildren(
    el("label", { text: "Failed, in order" }),
    el("div", { class: "chips" }, state.failures.map((code, i) => {
      counts[code] = (counts[code] || 0) + 1;
      const nth = counts[code] > 1 ? ` (attempt ${counts[code]})` : "";
      return el("span", { class: "chip" }, code + nth,
        el("button", {
          type: "button", class: "chip-x", "aria-label": `Undo failing ${code}`,
          text: "×",
          onclick: () => { state.failures.splice(i, 1); state.selected = null; drawPlan(); },
        }));
    })));
}

async function fillFailurePicker() {
  const select = $("add-failure");
  const codes = [];
  (state.lastResult?.periods || []).forEach((p) =>
    p.units.forEach((u) => codes.push(u)));
  codes.sort((a, b) => a.code.localeCompare(b.code));
  select.replaceChildren(
    el("option", { value: "", text: state.failures.length
      ? "Add another failure" : "Choose a unit you failed" }),
    ...codes.map((u) => el("option", {
      value: u.code, text: u.name ? `${u.code} ${u.name}` : u.code })));
}

/* Years are grouped from the period labels the engine returns rather than
   parsed for meaning. Both institutions label a period with its year. */
function byYear(periods) {
  const years = [];
  periods.forEach((period) => {
    const match = /^(Year\s+\d+)/.exec(period.period);
    const label = match ? match[1] : period.period;
    const tail = match
      ? period.period.slice(match[1].length).replace(/^\s*-\s*/, "") : "";
    let row = years.find((y) => y.label === label);
    if (!row) years.push((row = { label, periods: [] }));
    row.periods.push({ ...period, short: tail || period.period });
  });
  return years;
}

/* Moving a unit. The page rewrites the schedule it is holding and asks the
   engine what it thinks; it never decides the move is acceptable itself. */
async function applyArrangement(schedule) {
  const result = await post("/api/arrange", {
    institution: state.institution, key: state.key, schedule,
  });
  if (!result.ok) {
    $("plan-notices").replaceChildren(
      notice("problem", "That move is not possible", result.error));
    return;
  }
  state.schedule = result.schedule || schedule;
  state.edited = true;
  state.lastResult = result;
  render(result, {
    heading: result.valid
      ? "Your arrangement satisfies every recorded rule"
      : `Your arrangement breaks ${result.violation_count} rule${result.violation_count === 1 ? "" : "s"}`,
    kind: result.valid ? "ok" : "problem",
    detail: result.valid
      ? "Moved by you and checked by the engine."
      : null,
    items: result.valid ? null : (result.violations || []).map(violationText),
  });
}

/* Dragging, alongside clicking rather than instead of it.

   Click-to-swap stays because it is the path that works from a keyboard and on
   a touch screen, and because picking a unit up and putting it somewhere in two
   deliberate steps is easier to undo than a drag. Dragging is the faster way to
   do the same two things: onto a session moves the unit there, onto another
   unit trades their places. Neither asks whether the result is legal; both send
   the rearranged schedule to the validator and report what comes back, which is
   how a student can deliberately overload a session and be told what it costs. */

let dragging = null;      // the unit currently under the cursor
let dragged = false;      // set during a drag so the click it ends with is ignored

function swapUnits(a, b) {
  if (String(a.index) === String(b.index)) return null;
  const next = {};
  Object.entries(state.schedule).forEach(([i, codes]) => { next[i] = codes.slice(); });
  next[a.index] = next[a.index].filter((c) => c !== a.code);
  next[b.index] = next[b.index].filter((c) => c !== b.code);
  next[a.index].push(b.code);
  next[b.index].push(a.code);
  return next;
}

function moveUnit(a, index) {
  if (String(a.index) === String(index)) return null;
  const next = {};
  Object.entries(state.schedule).forEach(([i, codes]) => { next[i] = codes.slice(); });
  next[a.index] = next[a.index].filter((c) => c !== a.code);
  (next[index] = next[index] || []).push(a.code);
  return next;
}

function clearDropMarks() {
  document.querySelectorAll(".is-over").forEach((n) => n.classList.remove("is-over"));
}

function dragHandlers(code, index) {
  return {
    draggable: "true",
    ondragstart: (event) => {
      dragging = { code, index: String(index) };
      dragged = true;
      state.selected = null;
      event.dataTransfer.effectAllowed = "move";
      // Some browsers refuse to start a drag with nothing on the transfer.
      event.dataTransfer.setData("text/plain", code);
      event.currentTarget.classList.add("is-dragging");
    },
    ondragend: (event) => {
      event.currentTarget.classList.remove("is-dragging");
      clearDropMarks();
      dragging = null;
      setTimeout(() => { dragged = false; }, 0);
    },
    ondragover: (event) => {
      if (!dragging || dragging.code === code) return;
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = "move";
      event.currentTarget.classList.add("is-over");
    },
    ondragleave: (event) => event.currentTarget.classList.remove("is-over"),
    ondrop: (event) => {
      event.preventDefault();
      event.stopPropagation();
      clearDropMarks();
      if (!dragging || dragging.code === code) return;
      const next = swapUnits(dragging, { code, index: String(index) });
      dragging = null;
      if (next) applyArrangement(next);
    },
  };
}

function periodDropHandlers(index) {
  return {
    ondragover: (event) => {
      if (!dragging) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      event.currentTarget.classList.add("is-over");
    },
    ondragleave: (event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) {
        event.currentTarget.classList.remove("is-over");
      }
    },
    ondrop: (event) => {
      event.preventDefault();
      clearDropMarks();
      if (!dragging) return;
      const next = moveUnit(dragging, String(index));
      dragging = null;
      if (next) applyArrangement(next);
    },
  };
}

function pickUp(code, index) {
  if (state.selected && state.selected.code === code) {
    state.selected = null;
    drawCurrent();
    return;
  }
  if (!state.selected) {
    state.selected = { code, index };
    drawCurrent();
    return;
  }
  // Second click: trade the two units' positions.
  const a = state.selected, b = { code, index };
  state.selected = null;
  if (a.index === b.index) { drawCurrent(); return; }
  const next = {};
  Object.entries(state.schedule).forEach(([i, codes]) => { next[i] = codes.slice(); });
  next[a.index] = next[a.index].filter((c) => c !== a.code);
  next[b.index] = next[b.index].filter((c) => c !== b.code);
  next[a.index].push(b.code);
  next[b.index].push(a.code);
  applyArrangement(next);
}

function dropInto(index) {
  if (!state.selected) return;
  const a = state.selected;
  state.selected = null;
  if (String(a.index) === String(index)) { drawCurrent(); return; }
  const next = {};
  Object.entries(state.schedule).forEach(([i, codes]) => { next[i] = codes.slice(); });
  next[a.index] = next[a.index].filter((c) => c !== a.code);
  (next[index] = next[index] || []).push(a.code);
  applyArrangement(next);
}

function unitButton(unit, index) {
  const classes = ["unit"];
  if (unit.failed) classes.push("is-failed");
  else if (unit.moved) classes.push("is-moved");
  if (state.selected && state.selected.code === unit.code) classes.push("is-picked");
  const title = state.selected
    ? (state.selected.code === unit.code
       ? "click again to put it down" : `swap with ${state.selected.code}`)
    : unit.moved ? `moved from ${unit.moved.from}` : "click to move, or see its rules";
  return el("button", {
    class: classes.join(" "), type: "button", title,
    ...dragHandlers(unit.code, index),
    onclick: (event) => {
      if (dragged) return;        // the click a drag ends with is not a click
      if (event.shiftKey || event.metaKey || event.ctrlKey) openUnit(unit.code);
      else pickUp(unit.code, String(index));
    },
  },
    el("span", { class: "code", text: unit.code }),
    el("span", { class: "name", text: unit.name || "" }));
}

function drawGrid(result, nominalYears, isRepair) {
  const cap = result.maximum_credit_points;
  const standard = result.standard_credit_points;
  const years = byYear(result.periods);
  $("plan-grid").replaceChildren(...years.map((year, n) => {
    const extra = n >= nominalYears;
    return el("div", {
      class: "year" + (extra ? " is-extra" : "") + (extra && isRepair ? " is-new" : ""),
    },
      el("div", { class: "year-label" },
        el("b", { text: year.label.replace("Year ", "") }),
        extra ? "extra year" : "year"),
      el("div", { class: "periods" }, year.periods.map((period) => {
        // Above the maximum is a rule broken; above the standard load but
        // under the maximum is an overloaded session, which is allowed and
        // worth saying so a student who drags a unit in can see what it did.
        const over = cap ? period.credit_points > cap : false;
        const heavy = !over && standard ? period.credit_points > standard : false;
        return el("div", {
          class: "period" + (state.selected ? " is-target" : ""),
          ...periodDropHandlers(period.index),
          onclick: (event) => {
            if (dragged || event.target.closest(".unit")) return;
            dropInto(String(period.index));
          },
        },
          el("div", { class: "period-head" },
            el("span", { text: period.short }),
            el("span", {
              class: "cp" + (over ? " over" : heavy ? " heavy" : ""),
              title: over ? `above the ${cap} credit point maximum`
                   : heavy ? `above the usual ${standard} credit points` : null,
              text: `${period.credit_points} cp` })),
          period.units.map((u) => unitButton(u, period.index)),
          (period.failed_attempts || []).map((f) => el("div", { class: "attempt" },
            el("span", { class: "code", text: f.unit }),
            el("span", { class: "name", text: "failed attempt, still counts to the load" }))),
          (period.slots || []).map((slot) => el("div", { class: "slot",
            text: `${slot.credit_points} cp from ${slot.pool}` })));
      })));
  }));
}

function render(result, message) {
  $("plan-empty").hidden = true;
  $("plan-head").hidden = false;
  $("plan-title").textContent = tidy(result.title) || result.key;
  const total = result.total_credit_points
    || (result.periods || []).reduce((s, p) => s + p.credit_points, 0);
  $("plan-sub").textContent =
    `${total} credit points over ${(result.periods || []).length} teaching periods at ${instName()}`;
  $("plan-notices").replaceChildren(
    message ? notice(message.kind, message.heading, message.detail, message.items) : el("span"));
  $("hint-move").hidden = false;
  drawGrid(result, state.nominalYears || byYear(result.periods).length,
           state.failures.length > 0);
  drawFailureChips();
  fillFailurePicker();
  $("reset").hidden = !(state.failures.length || state.edited);
}

function drawCurrent() {
  if (state.lastResult) render(state.lastResult, state.lastMessage);
}

async function drawPlan() {
  if (!state.key) return;

  const base = await get("/api/plan", {
    institution: state.institution, key: state.key });
  if (!base.ok) {
    $("plan-notices").replaceChildren(
      notice("problem", "Could not load that plan", base.error));
    return;
  }
  state.nominalYears = byYear(base.periods).length;

  if (!state.failures.length) {
    state.schedule = base.schedule;
    state.lastResult = base;
    state.lastMessage = null;
    render(base, null);
    return;
  }

  // Each failure is applied to the plan produced by the one before it, which is
  // what lets a student fail a second unit, or the same unit twice.
  let result = null, schedule = null;
  for (let i = 0; i < state.failures.length; i += 1) {
    const code = state.failures[i];
    result = i === 0
      ? await get("/api/replan", { institution: state.institution,
                                   key: state.key, failed_unit: code })
      : await post("/api/replan-from", { institution: state.institution,
                                         key: state.key, failed_unit: code,
                                         schedule });
    if (!result.ok) {
      $("plan-notices").replaceChildren(notice("problem",
        `No repair found after failing ${code}`,
        result.error || "The engine could not place every unit."));
      state.failures.length = i;
      return;
    }
    schedule = result.schedule;
  }

  state.schedule = schedule;
  state.lastResult = result;

  const named = result.failed_unit_name
    ? `${result.failed_unit} ${result.failed_unit_name}` : result.failed_unit;
  const attempt = result.attempts_failed > 1
    ? `, failed ${result.attempts_failed} times now` : "";
  const moved = `${result.units_moved} unit${result.units_moved === 1 ? "" : "s"} moved.`;
  state.lastMessage = result.extends_plan
    ? { kind: "warn", heading: `Failing ${named}${attempt} extends the degree`,
        detail: `${moved} It now finishes in ${result.finishes_in} instead of `
          + `${result.original_finishes_in}. The engine finds a valid repair, `
          + `not always the shortest one.` }
    : { kind: "ok", heading: `Failing ${named}${attempt} fits inside the original span`,
        detail: `${moved} The degree still finishes in ${result.finishes_in}.` };
  render(result, state.lastMessage);
}

// ---------------------------------------------------------------- unit

async function openUnit(code) {
  const data = await get("/api/unit", { institution: state.institution, code });
  $("unit-code").textContent = code;
  $("unit-name").textContent = data.ok ? (data.name || "") : "";

  const facts = [];
  const add = (label, value, sub) => {
    if (value === null || value === undefined || value === "") return;
    facts.push(el("div", { class: "fact" },
      el("dt", { text: label }),
      el("dd", {}, String(value), sub ? el("small", { text: sub }) : null)));
  };
  if (!data.ok) {
    add("Not found", data.error || "This unit is not in the dataset.");
  } else {
    add("Credit points", data.credit_points);
    add("Offered", (data.sessions_offered || []).join(", ") || "not recorded");
    // The engine renders the requisite itself, from the fields its validator
    // reads, so this shows that sentence rather than composing a second one
    // here. Two renderings of the same rule are two things to keep in step, and
    // the AND of ORs in `prerequisite_groups` is easy to state backwards.
    const groups = data.prerequisite_groups || [];
    if (data.prerequisite_in_words && data.prerequisite_in_words !== "none recorded") {
      add("Prerequisite", data.prerequisite_in_words);
    } else if (groups.length) {
      add("Prerequisite", groups.map((g) => `(${g.join(" or ")})`).join(" and "));
    } else if ((data.prerequisite_alternatives || []).length) {
      add("Prerequisite", data.prerequisite_alternatives.join(" or "));
    }
    if (!data.prerequisite_in_words) {
      (data.credit_point_requirements || []).forEach((r) =>
        add("Needs beforehand", `${r.min_credit_points} cp`,
          r.min_level ? `at ${r.min_level} level or higher` : null));
    }
    if (data.corequisite_in_words && data.corequisite_in_words !== "none recorded") {
      add("Corequisite", data.corequisite_in_words);
    } else if ((data.corequisites || []).length) {
      add("Corequisite", data.corequisites.join(" or "));
    }
    if ((data.prohibitions || []).length) add("Cannot be taken with", data.prohibitions.join(", "));
    if (!data.requisites_recorded) {
      add("Requisites", "none recorded",
        "which means nothing was published to check, not that there are none");
    }
  }
  $("unit-facts").replaceChildren(...facts);
  $("unit-dialog").showModal();
}

$("unit-close").addEventListener("click", () => $("unit-dialog").close());

// ---------------------------------------------------------------- compare

const STRUCTURE_LABELS = {
  core: "Core", major: "Major", specialisation: "Specialisation",
  core_and_major_combined: "Core and major together",
  electives_or_specialist_stream: "Electives or specialist stream",
  professional_practice: "Professional practice",
};

async function loadPairs() {
  const data = await get("/api/pairs");
  state.pairs = data.pairs || [];
  $("pairs").replaceChildren(...state.pairs.map((pair) => el("button", {
    type: "button", "aria-pressed": "false", text: pair.degree,
    onclick: () => drawCompare(pair),
  })));
}

function money(value) {
  if (value === null || value === undefined) return null;
  return "$" + Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function facts(a, b) {
  const rows = [];
  const add = (label, left, right, note) => {
    if ((left ?? "") === "" && (right ?? "") === "") return;
    rows.push({ label, left, right, note,
                differs: String(left ?? "") !== String(right ?? "") });
  };
  const per = (s) => (s.length?.period_name || "period").replace(/s$/, "");
  const dom = (s) => s.tuition?.domestic_commonwealth_supported || {};
  const intl = (s) => s.tuition?.international || {};

  add("Award", a.award, b.award);
  add("Course code", a.course_code, b.course_code);
  add("Length", a.length && `${a.length.nominal_years} years`,
    b.length && `${b.length.nominal_years} years`);
  add("Teaching periods", a.length && `${a.length.teaching_periods} ${a.length.period_name}`,
    b.length && `${b.length.teaching_periods} ${b.length.period_name}`);
  add("Credit points", a.total_credit_points, b.total_credit_points);
  add("Usual load", a.load && `${a.load.standard_period_credit_points} cp`,
    b.load && `${b.load.standard_period_credit_points} cp`);
  add("Units a period", a.load && `about ${a.load.units_per_period_typical} a ${per(a)}`,
    b.load && `about ${b.load.units_per_period_typical} a ${per(b)}`);
  add("Most allowed", a.load && `${a.load.maximum_period_credit_points} cp`,
    b.load && `${b.load.maximum_period_credit_points} cp`);
  add("You choose", a.choice && `${a.choice.elective_credit_points} cp`,
    b.choice && `${b.choice.elective_credit_points} cp`, "in the combination shown");
  add("Specialisations offered", a.specialisations?.count, b.specialisations?.count);
  add("Domestic place, whole degree", money(dom(a).whole_degree), money(dom(b).whole_degree),
    dom(a).per_eftsl ? `${money(dom(a).per_eftsl)} a year, set nationally` : null);
  add("International, whole degree", money(intl(a).whole_degree), money(intl(b).whole_degree),
    `published as ${intl(a).as_published} and ${intl(b).as_published}`);
  add("International per credit point", money(intl(a).per_credit_point),
    money(intl(b).per_credit_point), "the only basis the two share");
  return rows;
}

function splitBlock(side, which) {
  const structure = side.credit_structure || {};
  return el("div", { class: `split-col ${which}` },
    el("h4", { text: side.institution === "USYD" ? "University of Sydney" : "UTS" }),
    el("dl", { class: "facts" }, Object.entries(structure)
      .filter(([, v]) => typeof v === "number")
      .map(([k, v]) => el("div", { class: "fact" },
        el("dt", { text: STRUCTURE_LABELS[k] || k.replace(/_/g, " ") }),
        el("dd", { text: `${v} cp` })))),
    structure.note ? el("p", { class: "caveat", text: structure.note }) : null);
}

function specBlock(side, which) {
  return el("div", { class: `split-col ${which}` },
    el("h4", { text: side.institution === "USYD" ? "University of Sydney" : "UTS" }),
    el("ul", { class: "pool" },
      (side.specialisations?.names || []).map((n) => el("li", { text: n }))),
    el("button", { class: "btn quiet", type: "button", style: "margin-top:14px",
      text: "Open this plan",
      onclick: () => openFromCompare(side.institution, side.key) }));
}

function openFromCompare(institution, key) {
  state.institution = institution;
  $("inst").querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.inst === institution)));
  const byDegree = state.grouped[institution] || {};
  const degree = Object.keys(byDegree).find((d) =>
    byDegree[d].some((o) => o.key === key));
  fillDegrees(degree);
  if (degree) {
    $("degree").value = degree; state.degree = degree;
    fillCombos(key); $("combo").value = key;
  }
  show("plan");
  window.scrollTo({ top: 0 });
}

async function drawCompare(pair) {
  $("pairs").querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.textContent === pair.degree)));
  const out = $("compare-out");
  out.replaceChildren(el("div", { class: "empty" }, "Working out the comparison."));

  const data = await get("/api/compare", {
    usyd_key: pair.usyd_key, uts_key: pair.uts_key });
  if (!data.ok) {
    out.replaceChildren(notice("problem", "Could not compare these degrees", data.error));
    return;
  }
  const diff = data.difference || {};
  const lines = [];
  if (diff.credit_point_difference) lines.push(
    `UTS requires ${Math.abs(diff.credit_point_difference)} credit points more.`);
  if (diff.elective_credit_point_difference) lines.push(
    `They differ by ${Math.abs(diff.elective_credit_point_difference)} credit points in how much you choose yourself.`);
  if (diff.domestic_cost_difference) lines.push(
    `The domestic cost differs by about ${money(Math.abs(diff.domestic_cost_difference))} over the degree.`);

  out.replaceChildren(
    lines.length ? notice("ok", `${pair.degree} engineering, side by side`, lines.join(" ")) : el("span"),
    el("div", { class: "ledger" },
      el("div", { class: "ledger-head" }, el("span"),
        el("span", { class: "usyd", text: "University of Sydney" }),
        el("span", { class: "uts", text: "UTS" })),
      facts(data.usyd, data.uts).map((row) =>
        el("div", { class: "ledger-row" + (row.differs ? " differs" : "") },
          el("div", { class: "ledger-label" }, row.label,
            row.note ? el("small", { text: row.note }) : null),
          el("div", { class: "ledger-value", "data-who": "Sydney",
                      text: row.left ?? "not published" }),
          el("div", { class: "ledger-value", "data-who": "UTS",
                      text: row.right ?? "not published" })))),
    el("h3", { class: "block-title", text: "How each publishes the credit-point split" }),
    el("p", { class: "caveat", text: "The two use different categories, so these are set out separately rather than forced onto shared rows." }),
    el("div", { class: "split-grid" }, splitBlock(data.usyd, "usyd"), splitBlock(data.uts, "uts")),
    el("h3", { class: "block-title", text: "Specialisations on offer" }),
    el("div", { class: "split-grid" }, specBlock(data.usyd, "usyd"), specBlock(data.uts, "uts")),
    el("p", { class: "caveat", text: data.note || "" }),
    data.fee_year_caveat ? el("p", { class: "caveat", text: data.fee_year_caveat }) : null);
}

// ---------------------------------------------------------------- ask

const EXAMPLES = [
  "Show me the plan for Mechatronic at Sydney with the Robotics and Intelligent Systems specialisation",
  "I'm in the Civil Engineering major at UTS and I failed Mathematics 1. What does my plan look like now?",
  "What are the prerequisites for AMME3500?",
  "How does Software Engineering at Sydney compare with the Software Engineering major at UTS?",
];

$("ask-examples").replaceChildren(...EXAMPLES.map((text) => el("button", {
  type: "button", text: text.length > 52 ? text.slice(0, 50) + "…" : text,
  title: text,
  onclick: () => { $("ask-text").value = text; $("ask-text").focus(); },
})));

async function checkModel() {
  const health = await get("/api/health");
  state.modelEnabled = !!health.model_enabled;
  $("ask-send").disabled = !state.modelEnabled;
  if (!state.modelEnabled) {
    $("thread").replaceChildren(notice("warn", "The conversation is switched off",
      "This run was started without the local model. The plan and compare views "
      + "do not use it and are unaffected."));
  }
}

/* The steps the system went through, in the order it went through them. This is
   not the model's own narration of its reasoning, which would be the model
   describing itself; it is what each stage actually produced, which is the
   division of labour of Section 3.9 made visible. */
function thinkingSteps(data) {
  const steps = [];
  const profile = data.profile || {};
  const bits = [];
  if (profile.institution) bits.push(profile.institution);
  if (profile.plan_key) bits.push(tidy(profile.plan_key));
  if ((profile.failed_units || []).length) bits.push(`failed ${profile.failed_units.join(", ")}`);
  if (profile.unit_of_interest) bits.push(`about ${profile.unit_of_interest}`);
  steps.push({
    who: "model", title: "Read the request",
    body: bits.length ? bits.join(" · ") : "could not tell what was being asked",
  });

  if (data.intent_override) {
    steps.push({ who: "rule", title: "Corrected the intent",
      body: `${data.intent_override.from} → ${data.intent_override.to}, `
        + "decided by rule because the extracted fields already settle it" });
  }
  if (profile.intent) {
    steps.push({ who: "rule", title: "Resolved against the dataset",
      body: `intent ${profile.intent}`
        + (profile.plan_key ? `, matched to ${tidy(profile.plan_key)}` : "") });
  }
  if (data.tool_call) {
    const args = Object.entries(data.tool_call.args || {})
      .map(([k, v]) => `${k}=${v}`).join(", ");
    steps.push({ who: "engine", title: `Called ${data.tool_call.tool}`, body: args || "no arguments" });
  }
  if (data.result) {
    const r = data.result;
    let summary;
    if (r.ok === false) summary = r.error || "the engine refused";
    else if (r.units_moved !== undefined) {
      summary = `${r.units_moved} units moved, finishes ${r.finishes_in}`;
    } else if (r.valid !== undefined) {
      summary = r.valid ? "the plan satisfies every recorded rule"
                        : `${r.violation_count} rule(s) not satisfied`;
    } else if (r.usyd && r.uts) summary = "compared the two degrees";
    else summary = "returned a result";
    steps.push({ who: "engine", title: "The engine answered", body: summary });
  }
  if (data.grounding) {
    const g = data.grounding;
    steps.push({
      who: g.grounded === false ? "flag" : "rule",
      title: g.grounded === false ? "The wording was flagged" : "Checked the wording",
      body: g.grounded === false
        ? `quoted ${[...(g.unsupported_codes || []), ...(g.unsupported_numbers || [])].join(", ")}, `
          + "which the engine did not produce"
        : "every code and figure in the reply appears in the engine's answer",
    });
  }
  return steps;
}

function stepNode(step) {
  return el("li", { class: `step ${step.who}` },
    el("span", { class: "step-who", text: step.who }),
    el("div", {}, el("b", { text: step.title }),
      step.body ? el("span", { text: " " + step.body }) : null));
}

/* Text is revealed a few characters at a time as it arrives. The pacing is
   presentation; the content is not. Chunks come from the model through the
   server as it produces them, and the queue only smooths the bursts into
   something readable, flushing whatever is left the moment the stream ends so
   the animation can never lag behind the real answer. */
function typer(node) {
  let pending = "", shown = "", running = false, finished = false;

  function tick() {
    if (!pending.length) {
      running = false;
      if (finished) node.classList.remove("typing");
      return;
    }
    const take = Math.max(2, Math.ceil(pending.length / 18));
    shown += pending.slice(0, take);
    pending = pending.slice(take);
    node.textContent = shown;
    requestAnimationFrame(tick);
  }

  return {
    push(piece) {
      pending += piece;
      node.classList.add("typing");
      if (!running) { running = true; requestAnimationFrame(tick); }
    },
    finish(fallback) {
      finished = true;
      if (!shown && !pending && fallback) {
        node.textContent = fallback;
        node.classList.remove("typing");
        return;
      }
      if (!running) { node.textContent = shown + pending; pending = ""; node.classList.remove("typing"); }
    },
  };
}

function detailsBlock(result) {
  return el("details", { class: "raw" },
    el("summary", { text: "The engine's answer, as returned" }),
    el("pre", { text: JSON.stringify(result, null, 1) }));
}

async function ask(message) {
  const steps = el("ol", { class: "steps" });
  const thinking = el("details", { class: "thinking", open: true },
    el("summary", { class: "live", text: "Working" }), steps);
  const reply = el("p", { class: "said" });
  const bubble = el("div", { class: "bubble from-system" }, thinking, reply);
  const turn = el("div", { class: "turn" },
    el("div", { class: "bubble from-you" }, el("p", { text: message })), bubble);

  const thread = $("thread");
  if (thread.querySelector(".empty")) thread.replaceChildren();
  thread.append(turn);
  turn.scrollIntoView({ behavior: "smooth", block: "end" });

  const write = typer(reply);
  let done = null, count = 0;

  const response = await fetch("/api/ask/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, context: state.context || null }),
  });

  if (!response.ok || !response.body) {
    const fallback = await response.json().catch(() => ({}));
    thinking.remove();
    reply.textContent = fallback.error || "The conversation is not available.";
    return;
  }

  // One JSON object per line. A chunk can split a line, so the tail is kept.
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done: finished } = await reader.read();
    if (finished) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      let event;
      try { event = JSON.parse(line); } catch { continue; }
      if (event.step) {
        count += 1;
        steps.append(stepNode(event.step));
        turn.scrollIntoView({ behavior: "smooth", block: "end" });
      } else if (event.text) {
        write.push(event.text);
      } else if (event.done) {
        done = event.done;
      }
    }
  }

  // The reply is only streamed when the model wrote one. An exchange that ends
  // in a clarifying question, or that the engine refused, produces a reply with
  // no streamed text at all, and showing "No answer." for those threw away a
  // perfectly good answer. The fallback is the reply the server returned.
  write.finish((done && (done.reply || done.error)) || "No answer.");
  thinking.open = false;
  thinking.querySelector("summary").classList.remove("live");
  thinking.querySelector("summary").textContent =
    count ? `How it got there, ${count} steps` : "How it got there";
  // A flagged reply quoted a code or a figure the engine did not produce, so it
  // says so where the student is actually reading rather than only inside the
  // steps. The prose is left in place: the engine's own answer is underneath it
  // and is the thing to trust, which is the point the notice makes.
  if (done && done.grounding && done.grounding.grounded === false) {
    const quoted = [...(done.grounding.unsupported_codes || []),
                    ...(done.grounding.unsupported_numbers || [])].join(", ");
    bubble.insertBefore(
      el("p", { class: "ungrounded" },
        el("b", { text: "Do not rely on this wording. " }),
        `It quotes ${quoted}, which the engine did not produce. `
        + "The engine's own answer is below."),
      reply.nextSibling);
  }

  if (done && done.result) bubble.append(detailsBlock(done.result));

  // The resolved profile becomes the context for the next question, so a
  // follow-up need not repeat the degree. The transcript is not kept: what
  // carries forward is the structured reading, which is what keeps the pair of
  // (context, question) enough to determine the answer.
  if (done && done.ok && done.profile) state.context = done.profile;
  if (done && (done.carried_from_context || []).length) {
    turn.querySelector(".from-you").append(
      el("span", { class: "carried",
                   text: "using the degree from your last question" }));
  }
}

$("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("ask-text").value.trim();
  if (!message) return;
  $("ask-text").value = "";
  $("ask-text").style.height = "auto";
  $("ask-send").disabled = true;
  try {
    await ask(message);
  } finally {
    $("ask-send").disabled = !state.modelEnabled;
    $("ask-text").focus();
  }
});

$("ask-reset").addEventListener("click", () => {
  state.context = null;
  $("thread").replaceChildren(el("div", { class: "empty" },
    el("h2", { text: "Ask about a plan" }),
    el("p", { text: "The model reads your question and calls the engine. It "
      + "never decides whether a plan is valid, and each answer shows the steps "
      + "it took to get there." })));
});

$("ask-text").addEventListener("input", (event) => {
  event.target.style.height = "auto";
  event.target.style.height = Math.min(event.target.scrollHeight, 180) + "px";
});

$("ask-text").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("ask-form").requestSubmit();
  }
});

// ---------------------------------------------------------------- start

loadPlans();
show("plan");
