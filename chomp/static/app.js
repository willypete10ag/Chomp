/* Chomp dashboard: small enhancements on top of plain forms, which all work without this
   file. Sliders mirror their number boxes, quick-pick chips fill them in, and previews show
   what a change will do before it is saved. The preview math repeats logic.scale on the
   numbers the page was rendered with; what gets saved is always computed by the server. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  var PLACES = { kcal: 0, protein_g: 1, carbs_g: 1, fat_g: 1 };
  var KCAL_PER_G = { protein_g: 4, carbs_g: 4, fat_g: 9 };

  function parse(value) {
    var n = parseFloat(String(value).replace(/,/g, ""));
    return isFinite(n) ? n : null;
  }

  function fmt(value, places) {
    return value.toLocaleString("en-US", { maximumFractionDigits: places });
  }

  function json(el, name) {
    try {
      return el.dataset[name] ? JSON.parse(el.dataset[name]) : null;
    } catch (err) {
      return null;
    }
  }

  // Food previews: "= 214 kcal · P 18.9 C 1.1 F 14.3", and the day's calories after saving
  function foodPreview(form) {
    var per = json(form, "per100");
    var out = form.querySelector(".preview");
    var field = form.querySelector("[data-amount]");
    if (!per || !out || !field) return;
    var grams = parse(field.value);
    var ok = grams !== null && grams > 0;
    Object.keys(PLACES).forEach(function (k) {
      var el = out.querySelector('[data-n="' + k + '"]');
      if (el) el.textContent = ok ? fmt((per[k] * grams) / 100, PLACES[k]) : "–";
    });
    var day = out.querySelector("[data-day]");
    if (day) {
      var base = parse(form.dataset.base) || 0;
      var goal = parse(form.dataset.goal);
      var total = base + (ok ? (per.kcal * grams) / 100 : 0);
      day.textContent = fmt(total, 0) + (goal ? " of " + fmt(goal, 0) : "") + " kcal";
      day.classList.toggle("warn", !!goal && total > goal);
    }
  }

  // Goals: what the protein, carbs and fat goals add up to, and each one's share
  function goalsPreview(form) {
    var hint = form.querySelector("[data-macro-kcal]");
    if (!hint) return;
    var parts = {};
    var total = 0;
    var complete = Object.keys(KCAL_PER_G).every(function (k) {
      var v = parse((form.elements[k] || {}).value);
      if (v === null || v <= 0) return false;
      parts[k] = v * KCAL_PER_G[k];
      total += parts[k];
      return true;
    });
    var match = form.querySelector("[data-match]");
    form.querySelector(".split").hidden = !complete || total <= 0;
    if (match) match.hidden = !complete || total <= 0;
    if (!complete || total <= 0) return;
    hint.textContent = fmt(total, 0);
    if (match) {
      match.dataset.set = String(Math.round(total));
      match.textContent = "Match protein, carbs and fat · " + fmt(total, 0) + " kcal";
    }
    var x = 0;
    Object.keys(KCAL_PER_G).forEach(function (k) {
      var share = (100 * parts[k]) / total;
      var rect = form.querySelector('.split rect[data-k="' + k + '"]');
      if (rect) {
        rect.setAttribute("x", x.toFixed(1));
        rect.setAttribute("width", share.toFixed(1));
      }
      var pct = form.querySelector('[data-pct="' + k + '"]');
      if (pct) pct.textContent = Math.round(share) + "%";
      x += share;
    });
    var kcal = parse((form.elements.kcal || {}).value);
    var diff = form.querySelector("[data-kcal-diff]");
    if (diff) {
      var gap = kcal ? Math.round(total - kcal) : 0;
      diff.textContent = !kcal || Math.abs(gap) < 25 ? "" :
        " (" + fmt(Math.abs(gap), 0) + (gap > 0 ? " over" : " under") + " your calorie goal)";
    }
  }

  function changed(form) {
    if (!form) return;
    foodPreview(form);
    goalsPreview(form);
  }

  // A slider and its number box move together
  document.querySelectorAll(".amount").forEach(function (box) {
    var range = box.querySelector("input[type=range]");
    var field = box.querySelector("[data-amount]");
    if (!range || !field) return;
    range.addEventListener("input", function () {
      field.value = range.value;
      changed(field.form);
    });
    field.addEventListener("input", function () {
      var n = parse(field.value);
      if (n !== null) {
        if (n > parse(range.max)) range.max = String(Math.ceil(n));
        range.value = String(n);
      }
      changed(field.form);
    });
  });

  // Quick picks: "1 serving · 46 g", "Your usual · 300 g", "100 g"
  document.addEventListener("click", function (event) {
    var chip = event.target.closest("[data-set]");
    if (!chip) return;
    var box = chip.closest(".goal, form").querySelector(".amount");
    var field = box.querySelector("[data-amount]");
    field.value = chip.dataset.set;
    field.dispatchEvent(new Event("input"));
  });

  // Time nudges on meal times: -15 min, +15 min, now
  document.addEventListener("click", function (event) {
    var chip = event.target.closest("[data-nudge]");
    if (!chip) return;
    var field = chip.closest("form").querySelector("input[type=time]");
    var step = chip.dataset.nudge;
    var minutes;
    if (step === "now") {
      var now = new Date();
      minutes = now.getHours() * 60 + now.getMinutes();
    } else {
      var parts = (field.value || "12:00").split(":");
      minutes = (parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10) + parseInt(step, 10)
        + 1440) % 1440;
    }
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    field.value = pad(Math.floor(minutes / 60)) + ":" + pad(minutes % 60);
  });

  // Close a confirmation banner
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-dismiss]");
    if (button) button.closest(".flash").remove();
  });

  // "Move to" and "Add to" menus: the time box only matters for a new meal
  document.querySelectorAll("select[data-new-meal]").forEach(function (select) {
    var time = select.form.querySelector(".new-meal");
    var sync = function () {
      if (time) time.hidden = select.value !== "new";
    };
    select.addEventListener("change", sync);
    sync();
  });

  // New food from a label photo: count the ticked values and say whether saving now gives
  // a confirmed food (every filled-in value ticked) or an unconfirmed one
  document.querySelectorAll("form[data-ticks]").forEach(function (form) {
    var count = form.querySelector("[data-tick-count]");
    var save = form.querySelector("[data-save]");
    var sync = function () {
      var filled = 0;
      var ticked = 0;
      form.querySelectorAll(".vrow").forEach(function (row) {
        var box = row.querySelector("input[type=number]");
        var tick = row.querySelector("input[type=checkbox]");
        if (!box || !tick || box.value.trim() === "") return;
        filled += 1;
        if (tick.checked) ticked += 1;
      });
      var all = filled > 0 && ticked === filled;
      count.textContent = ticked + " of " + filled + " values ticked" +
        (all ? ". Saves as confirmed." : ". Tick every value to save it as confirmed.");
      save.textContent = all ? "Save as confirmed" : "Save as unconfirmed";
    };
    form.addEventListener("input", sync);
    form.addEventListener("change", sync);
    sync();
  });

  document.querySelectorAll("form").forEach(changed);
})();
