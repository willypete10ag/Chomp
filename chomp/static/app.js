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

  // The grams a food form will log: its servings times the serving size when Servings is
  // picked (#80), else its grams box
  function amountGrams(form) {
    var by = form.querySelector("input[name=by]:checked");
    if (by && by.value === "servings") {
      var box = form.querySelector("[data-servings]");
      var n = parse(box.value);
      return n !== null ? n * parse(box.closest("[data-serving]").dataset.serving) : null;
    }
    var field = form.querySelector("[data-amount]");
    return field ? parse(field.value) : null;
  }

  // Food previews: "= 214 kcal · P 18.9 C 1.1 F 14.3", and the day's calories after saving
  function foodPreview(form) {
    var per = json(form, "per100");
    var out = form.querySelector(".preview");
    if (!per || !out || !form.querySelector("[data-amount]")) return;
    var grams = amountGrams(form);
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

  // Goals: one calorie bar split into protein, carbs and fat. Dragging a border trades
  // calories between the two slices beside it while the total stays the number typed above
  // the bar; a new number starts the slices at even thirds. Grams move in 1 g steps and no
  // slice gets under 5%. The math repeats logic.fit_macros; the server checks what's saved.
  var MACRO_KEYS = ["protein_g", "carbs_g", "fat_g"];
  var MIN_SHARE = 0.05;

  function calorieBar(form) {
    var bar = form.querySelector("[data-calbar]");
    if (!bar) return;
    var kcalField = form.elements.kcal;
    var handles = bar.querySelectorAll("[data-handle]");
    var grams = {};
    MACRO_KEYS.forEach(function (k) {
      grams[k] = parse(form.elements[k].value) || 0;
    });

    function total() {
      var n = parse(kcalField.value);
      return n !== null && n > 0 ? n : null;
    }

    function clamp(n, lo, hi) {
      return Math.max(lo, Math.min(hi, n));
    }

    // Carbs take whatever protein and fat leave
    function settle(kcal) {
      var rest = kcal - grams.protein_g * 4 - grams.fat_g * 9;
      grams.carbs_g = Math.max(Math.round(rest / 4), 0);
    }

    function thirds(kcal) {
      grams.protein_g = Math.round(kcal / 3 / 4);
      grams.fat_g = Math.round(kcal / 3 / 9);
      settle(kcal);
    }

    // which 0 is the protein | carbs border (sets protein), 1 the carbs | fat one (sets fat)
    function setBorder(which, g) {
      var kcal = total();
      if (!kcal) return;
      var key = which === 0 ? "protein_g" : "fat_g";
      var other = which === 0 ? "fat_g" : "protein_g";
      var per = KCAL_PER_G[key];
      var lo = Math.ceil((MIN_SHARE * kcal) / per);
      var hi = Math.floor(((1 - MIN_SHARE) * kcal - grams[other] * KCAL_PER_G[other]) / per);
      grams[key] = clamp(Math.round(g), lo, hi);
      settle(kcal);
      draw();
    }

    function draw() {
      var parts = {};
      var sum = 0;
      MACRO_KEYS.forEach(function (k) {
        parts[k] = grams[k] * KCAL_PER_G[k];
        sum += parts[k];
      });
      if (sum <= 0) return;
      var x = 0;
      var out = false;
      MACRO_KEYS.forEach(function (k, i) {
        var share = parts[k] / sum;
        var rect = bar.querySelector('rect[data-k="' + k + '"]');
        rect.setAttribute("x", (100 * x).toFixed(2));
        rect.setAttribute("width", (100 * share).toFixed(2));
        var lab = bar.querySelector('[data-lab="' + k + '"]');
        lab.style.left = 100 * x + "%";
        lab.style.width = 100 * share + "%";
        lab.querySelector("[data-g]").textContent = fmt(grams[k], 1);
        lab.querySelector("[data-pct]").textContent = Math.round(100 * share);
        lab.classList.remove("out");
        if (lab.querySelector(".txt").offsetWidth > lab.clientWidth - 8) {
          lab.classList.add("out");
          out = true;
        }
        form.elements[k].value = String(grams[k]);
        x += share;
        if (i < 2) {
          var h = handles[i];
          h.style.left = 100 * x + "%";
          var g = grams[i === 0 ? "protein_g" : "fat_g"];
          h.setAttribute("aria-valuenow", String(g));
          h.setAttribute("aria-valuetext", fmt(g, 1) + " grams of " + (i === 0 ? "protein" : "fat"));
        }
      });
      bar.classList.toggle("has-out", out);
    }

    handles.forEach(function (h) {
      var which = parseInt(h.dataset.handle, 10);
      function follow(event) {
        var kcal = total();
        if (!kcal) return;
        var r = bar.getBoundingClientRect();
        var pos = clamp((event.clientX - r.left) / r.width, 0, 1);
        setBorder(which, which === 0 ? (pos * kcal) / 4 : ((1 - pos) * kcal) / 9);
      }
      h.addEventListener("pointerdown", function (event) {
        h.setPointerCapture(event.pointerId);
        h.classList.add("drag");
        event.preventDefault();
      });
      h.addEventListener("pointermove", function (event) {
        if (h.hasPointerCapture(event.pointerId)) follow(event);
      });
      ["pointerup", "pointercancel"].forEach(function (type) {
        h.addEventListener(type, function () {
          h.classList.remove("drag");
        });
      });
      // Arrow keys move the border 1 g at a time
      h.addEventListener("keydown", function (event) {
        var step = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 }[event.key];
        if (!step) return;
        event.preventDefault();
        if (which === 0) setBorder(0, grams.protein_g + step);
        else setBorder(1, grams.fat_g - step);
      });
    });

    kcalField.addEventListener("input", function () {
      var kcal = total();
      if (!kcal) return;
      thirds(kcal);
      var note = form.querySelector("[data-refit]");
      if (note) note.hidden = true;
      draw();
    });
    window.addEventListener("resize", draw);
    draw();
  }

  document.querySelectorAll("form.goals").forEach(calorieBar);

  function changed(form) {
    if (!form) return;
    foodPreview(form);
  }

  // A slider and its number box move together
  document.querySelectorAll(".amount").forEach(function (box) {
    var range = box.querySelector("input[type=range]");
    var field = box.querySelector("[data-amount]");
    if (!range || !field) return;
    // Grams chosen by hand are exact; an estimated portion chip marks them again (#75)
    var estimated = field.form && field.form.querySelector("input[name=estimated]");
    range.addEventListener("input", function () {
      field.value = range.value;
      if (estimated) estimated.value = "0";
      syncServings(field.form, parse(range.value));
      changed(field.form);
    });
    field.addEventListener("input", function () {
      if (estimated) estimated.value = "0";
      var n = parse(field.value);
      if (n !== null) {
        if (n > parse(range.max)) range.max = String(Math.ceil(n));
        range.value = String(n);
      }
      syncServings(field.form, n);
      changed(field.form);
    });
  });

  // Servings (#80): the servings box and the grams box show the same amount, whichever is
  // changed; the Servings / Grams toggle says which one gets logged
  function countText(n) {
    return String(Math.round(n * 100) / 100);
  }
  function syncServings(form, grams) {
    var box = form && form.querySelector("[data-servings]");
    if (!box || grams === null || grams <= 0) return;
    box.value = countText(grams / parse(box.closest("[data-serving]").dataset.serving));
  }
  document.querySelectorAll("[data-servings]").forEach(function (box) {
    var form = box.form;
    var size = parse(box.closest("[data-serving]").dataset.serving);
    var field = form.querySelector("[data-amount]");
    var range = field.closest(".amount").querySelector("input[type=range]");
    var estimated = form.querySelector("input[name=estimated]");
    box.addEventListener("input", function () {
      if (estimated) estimated.value = "0";
      var n = parse(box.value);
      if (n !== null && n > 0) {
        var grams = Math.round(n * size * 10) / 10;
        field.value = String(grams);
        if (range) {
          if (grams > parse(range.max)) range.max = String(Math.ceil(grams));
          range.value = String(grams);
        }
      }
      changed(form);
    });
    // − and + move to the next half serving down or up (1.76 goes to 1.5 or 2), not under ½
    box.closest(".field").querySelectorAll("[data-step]").forEach(function (button) {
      button.addEventListener("click", function () {
        var n = parse(box.value) || 0;
        var next = button.dataset.step === "1"
          ? Math.floor(n * 2 + 1e-9) / 2 + 0.5
          : Math.ceil(n * 2 - 1e-9) / 2 - 0.5;
        box.value = countText(Math.max(next, 0.5));
        box.dispatchEvent(new Event("input"));
      });
    });
  });
  document.addEventListener("change", function (event) {
    if (event.target.name === "by") changed(event.target.form);
  });

  // Quick picks: "1 serving · 46 g", "1 handful · ~30 g", "100 g"
  document.addEventListener("click", function (event) {
    var chip = event.target.closest("[data-set]");
    if (!chip) return;
    var field = chip.closest(".goal, form").querySelector("[data-amount]");
    field.value = chip.dataset.set;
    // Chips are weights, so a tap on one while Servings is picked switches to Grams
    var grams = field.form && field.form.querySelector("input[name=by][value=grams]");
    if (grams) grams.checked = true;
    field.dispatchEvent(new Event("input"));
    var estimated = field.form && field.form.querySelector("input[name=estimated]");
    if (estimated && chip.dataset.estimated) estimated.value = "1";
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

  // Foods and Breakdown: remember which cards were folded (1) or opened (0), in this
  // browser only
  var folded = {};
  try {
    folded = JSON.parse(localStorage.getItem("chomp-folded") || "{}") || {};
  } catch (err) {
    folded = {};
  }
  document.querySelectorAll("details[data-fold]").forEach(function (fold) {
    var name = fold.dataset.fold;
    if (folded[name] === 1) fold.open = false;
    else if (folded[name] === 0) fold.open = true;
    fold.addEventListener("toggle", function () {
      folded[name] = fold.open ? 0 : 1;
      try {
        localStorage.setItem("chomp-folded", JSON.stringify(folded));
      } catch (err) {
        // private browsing: folding still works, it just isn't remembered
      }
    });
  });

  // Breakdown: a link to a section (tapping Protein on Today) opens the card it's in
  var reveal = function () {
    var target = location.hash && document.getElementById(location.hash.slice(1));
    if (!target) return;
    var fold = target.closest("details[data-fold]");
    if (fold && !fold.open) fold.open = true;
    target.scrollIntoView();
  };
  reveal();
  window.addEventListener("hashchange", reveal);

  // Foods: swipe a food left to show its Delete button; swipe back, tap it or open
  // another to hide it again. Only one shows at a time. Press and hold a food to open it
  // (details and Rename); a plain tap only closes an open one. Portions in a food's
  // details work the same way: swipe to delete, press and hold to edit.
  var OPEN = 96;
  var HOLD_MS = 450;
  var shown = null;
  function slide(body, x) {
    body.style.transform = x ? "translateX(" + x + "px)" : "";
  }
  function hide() {
    if (shown) slide(shown, 0);
    shown = null;
  }
  document.addEventListener("pointerdown", function (event) {
    if (shown && !shown.parentNode.contains(event.target)) hide();
  });
  document.querySelectorAll("[data-swipe]").forEach(function (body) {
    var start = null;
    var dx = 0;
    var sliding = false;
    var details = body.querySelector("details");
    var summary = details.querySelector("summary");
    var timer = null;
    var held = false;
    function unhold() {
      clearTimeout(timer);
      timer = null;
      summary.classList.remove("holding");
    }
    body.addEventListener("pointerdown", function (event) {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      // A portion inside an open food swipes and holds on its own, not the food with it
      if (event.target.closest("[data-swipe]") !== body) return;
      start = { x: event.clientX, y: event.clientY, base: body === shown ? -OPEN : 0 };
      dx = 0;
      sliding = false;
      held = false;
      unhold();
      if (body !== shown && !details.open && summary.contains(event.target)) {
        summary.classList.add("holding");
        timer = setTimeout(function () {
          unhold();
          held = true;
          details.open = true;
          var rename = details.querySelector("details.sub.rename");
          if (rename) rename.open = true;
          if (navigator.vibrate) navigator.vibrate(15);
        }, HOLD_MS);
      }
    });
    // A long press shouldn't select text or pop up the phone's menu
    summary.addEventListener("contextmenu", function (event) {
      if (timer || held) event.preventDefault();
    });
    body.addEventListener("pointermove", function (event) {
      if (!start) return;
      var mx = event.clientX - start.x;
      var my = event.clientY - start.y;
      if (Math.abs(mx) > 8 || Math.abs(my) > 8) unhold();
      if (!sliding) {
        if (Math.abs(mx) < 8 || Math.abs(mx) < Math.abs(my)) {
          if (Math.abs(my) > 8) start = null; // a scroll, not a swipe
          return;
        }
        sliding = true;
        if (shown && shown !== body) hide();
        body.classList.add("dragging");
        try {
          body.setPointerCapture(event.pointerId);
        } catch (err) {
          // capture is a nicety
        }
      }
      dx = Math.max(-OPEN - 24, Math.min(0, start.base + mx));
      slide(body, dx);
    });
    function end() {
      unhold();
      if (!start) return;
      body.classList.remove("dragging");
      if (sliding) {
        if (dx < -OPEN / 2) {
          slide(body, -OPEN);
          shown = body;
        } else {
          slide(body, 0);
          if (shown === body) shown = null;
        }
      }
      start = null;
    }
    body.addEventListener("pointerup", end);
    body.addEventListener("pointercancel", end);
    // The tap that ends a swipe, or a tap on a food showing Delete, doesn't open it. Nor
    // does a plain tap (only a long press does), though the keyboard's Enter still does.
    body.addEventListener("click", function (event) {
      if (sliding || body === shown) {
        event.preventDefault();
        event.stopPropagation();
        if (!sliding) hide();
        sliding = false;
        held = false;
        return;
      }
      if (!summary.contains(event.target)) return;
      if (held || (!details.open && event.detail > 0)) event.preventDefault();
      held = false;
    }, true);
    // Tabbing onto Delete slides the food aside so the button is seen before it's pressed
    var action = body.parentNode.querySelector(".swipe-action");
    if (action) {
      action.addEventListener("focusin", function () {
        if (shown && shown !== body) hide();
        slide(body, -OPEN);
        shown = body;
      });
      action.addEventListener("focusout", function () {
        if (shown === body) hide();
      });
    }
  });

  document.querySelectorAll("form").forEach(changed);
})();
